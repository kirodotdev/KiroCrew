"""Static plan preview for a dynamic-workflow script — the graph view's "planned" half.

A run's event stream says what DID happen. This module says what the script SAYS it
will do, read off the already-validated source with ``ast`` and nothing else, so the
Workflows graph can draw a plan before the first agent starts and then light it up.

Layer: leaf within the package -- no intra-package imports (GATE F1, enforced by
``test/test_workflows_architecture.py``). Outside the package it depends on
``kiro_crew.security`` alone, because a string this module RETAINS must be redacted
before it is cut: see ``_bounded`` below.

WHAT IS PREDICTED, AND WHAT DELIBERATELY IS NOT
-----------------------------------------------
``ctx.phase("title")`` is the only structural anchor a script gives, and phases run
in source order, so a plan is a list of phases each holding a list of nodes.

A construct whose SHAPE depends on a runtime value is never guessed. It contributes
exactly one ``unknown`` node naming the construct, and every node found inside it is
marked ``certain: False``:

* ``if`` / ``for`` / ``while`` / ``async for`` / ``try`` / ``match``, a conditional
  expression, and a comprehension — may not run, or may run a number of times only
  the run knows.
* ``ctx.parallel(<not a literal list or tuple>)`` — fan-out width is a runtime value.
* ``ctx.pipeline(...)`` — width times stage count, both runtime.
* ``ctx.workflow(...)`` — a nested workflow, whose own body is not read here.
* ``ctx.phase(<not a literal string>)`` — work certainly happens, under a name only
  the run knows, so it cannot be paired with a ``phase_started`` event at all.
* ``<helper>(...)`` where ``helper`` is defined in the script — the validator
  deliberately does not scan helper bodies for the ``ctx`` surface, and neither does
  this, so the region is reported as unpredictable rather than as empty.

That single rule is what bounds how wrong the preview can be: it never draws a node
count it cannot justify, and an ``unknown`` node is a fence the consumer must not
match positions across (``planModel.ts`` stops its ordinal pairing there).

A missing or unparseable entrypoint yields ``None``, not an empty plan — "we have no
plan" and "the plan is empty" must not look the same to the UI.
"""

from __future__ import annotations

import ast
from typing import Any, Optional, Union

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Ceilings. A workflow script is LLM-authored and may be up to ``MAX_SCRIPT_BYTES``,
# so a pathological body (nested loops each holding agent calls) could otherwise emit
# thousands of nodes into an HTTP response nobody can draw. Hitting either cap sets
# ``truncated`` so the UI can say the plan is partial rather than claim it is whole.
MAX_PLAN_PHASES = 50
MAX_PLAN_NODES = 200

# Longest label kept on a node. Labels come from the script's own string literals,
# which are LLM-authored. The response passes through the handler's redactor and the
# frontend sanitizes again, but neither can help once a cut has split a credential into
# a fragment their patterns do not match -- so the cut itself is ordered behind
# redaction, in ``_bounded``.
MAX_LABEL_CHARS = 60

# Longest phase title kept on a plan row. Same reason as MAX_LABEL_CHARS: the title is
# an LLM-authored literal, and MAX_PLAN_PHASES bounds only how MANY rows are retained,
# not how long each one is. The bound travels to the consumer as ``titleLimit`` so a
# phase whose title was cut here still pairs with its ``phase_started`` event, which
# the runner emits untruncated.
MAX_PHASE_TITLE_CHARS = 120

# The runner's OWN cut for an agent whose label falls back to the prompt: it emits
# ``label or prompt[:40]``. A preview that reached for MAX_LABEL_CHARS here would draw
# sixty characters where the run then shows forty, so the planned box would rename
# itself the moment it lit up. ``test_runner_prompt_label_cut_has_not_moved`` fails if
# the runner's forty ever changes.
RUNNER_PROMPT_LABEL_CHARS = 40

# Every ``ctx`` method this module draws something for. Read by ``_visit_call``, so it
# cannot drift from the dispatch it describes.
MODELLED_CTX_METHODS = frozenset({"agent", "parallel", "pipeline", "workflow", "phase"})

# Methods that draw nothing on purpose: narration and side channels, not units of work.
# Enumerated rather than implied so each one is a decision on the record.
#
# Together the two sets cover the ``WorkflowContext`` Protocol exactly. That Protocol is
# already frozen by test_workflows_conformance.py, but a freeze on the CONTRACT says
# nothing about whether this module classified a method added to it -- so a deliberate
# re-freeze could add work-spawning verb and leave the previewer drawing nothing for it.
# The parity test in test_workflows_preview.py closes that gap, and ``_visit_call``
# degrades an unclassified method to an unpredictable marker so the runtime is honest
# even if the pin is ever relaxed.
NARRATION_CTX_METHODS = frozenset({"log", "nudge", "approve", "send_slack", "send_message"})


def _bounded(value: str, limit: int) -> str:
    """Redact *value*, THEN cut it to *limit*.

    The order is the whole point. Every string here is an LLM-authored literal, and the
    response-level redactors match a credential by its full shape, so cutting first can
    leave a fragment no later pass matches -- the run's own source may legitimately
    carry a token, and a graph node is where it would surface. Redacting first means the
    cut only ever lands in text that has already been made safe.
    """
    safe, _ = redact_exfiltration_urls(value)
    safe, _ = redact_credentials(safe)
    return safe[:limit]


# Statement types whose body runs a number of times, or not at all, that only the run
# knows. Each contributes one ``unknown`` node and makes everything inside uncertain.
_BRANCHING_STMTS: tuple[tuple[type, str], ...] = (
    (ast.If, "if"),
    (ast.For, "for"),
    (ast.AsyncFor, "for"),
    (ast.While, "while"),
    (ast.Try, "try"),
    (ast.Match, "match"),
)

# Expression forms with the same property: a branch not taken, or a loop of unknown
# width, sits inside them.
_BRANCHING_EXPRS: tuple[tuple[type, str], ...] = (
    (ast.IfExp, "if"),
    (ast.ListComp, "for"),
    (ast.SetComp, "for"),
    (ast.DictComp, "for"),
    (ast.GeneratorExp, "for"),
)

# The entrypoint every workflow script must define (enforced by ``validate``).
_ENTRYPOINT = "workflow"

# Title of the phase that holds work appearing before the first ``ctx.phase()``. The
# runner tags those events with an empty phase, so the consumer pairs them by the
# same empty title.
_LEADING_PHASE = ""

_FuncDef = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def _literal_str(node: Optional[ast.AST]) -> Optional[str]:
    """The value of *node* when it is a plain string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ctx_method(node: ast.AST, ctx_name: str) -> Optional[str]:
    """Method name when *node* is a ``<ctx_name>.<method>(...)`` call, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == ctx_name
    ):
        return func.attr
    return None


def _unwrap(node: ast.AST) -> ast.AST:
    """Strip ``await`` so a call is recognised whether or not it is awaited."""
    while isinstance(node, ast.Await):
        node = node.value
    return node


def _agent_label(call: ast.Call) -> str:
    """Best label for an ``agent`` node, mirroring the runner's own choice.

    The runner emits ``label or prompt[:40]``, and each word of that expression matters
    here. ``or`` means an EMPTY ``label=""`` loses to the prompt exactly as a missing one
    does. ``prompt`` is positional-or-keyword in the DSL, so both spellings are read.
    ``[:40]`` is the runner's cut, not MAX_LABEL_CHARS, which bounds a label the runner
    never cut. Getting any of the three wrong renames the planned box the moment it runs.

    Redaction still outranks byte-parity: ``_bounded`` scrubs before cutting, so a prompt
    carrying a credential yields a label the run will not match. That is the intended
    trade -- the alternative is publishing the fragment.

    When neither label nor prompt is a literal the node still exists, because the call
    certainly does; it simply has no name to show.
    """
    for kw in call.keywords:
        if kw.arg == "label":
            literal = _literal_str(kw.value)
            if literal:
                return _bounded(literal, MAX_LABEL_CHARS)
    prompt: Optional[ast.expr] = call.args[0] if call.args else None
    if prompt is None:
        for kw in call.keywords:
            if kw.arg == "prompt":
                prompt = kw.value
                break
    if prompt is not None:
        literal = _literal_str(prompt)
        if literal is not None:
            return _bounded(literal, RUNNER_PROMPT_LABEL_CHARS)
    return ""


def _local_function_names(entry: ast.AST) -> set[str]:
    """Names bound by a ``def`` / ``async def`` anywhere inside the entrypoint.

    A call to one of these reaches a body this module does not read, so it is
    reported as unpredictable rather than silently contributing nothing.
    """
    names: set[str] = set()
    for child in ast.walk(entry):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(child.name)
    return names


def _find_entrypoint(tree: ast.Module) -> Optional[_FuncDef]:
    """The module-level ``workflow`` function, sync or async, else None."""
    for stmt in tree.body:
        if isinstance(stmt, (ast.AsyncFunctionDef, ast.FunctionDef)) and stmt.name == _ENTRYPOINT:
            return stmt
    return None


def _branch_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Every statement list a branching construct can execute."""
    bodies: list[list[ast.stmt]] = []
    for attr in ("body", "orelse", "finalbody"):
        block = getattr(stmt, attr, None)
        if isinstance(block, list) and block:
            bodies.append(block)
    for handler in getattr(stmt, "handlers", None) or []:
        if handler.body:
            bodies.append(handler.body)
    for case in getattr(stmt, "cases", None) or []:
        if case.body:
            bodies.append(case.body)
    return bodies


class _Planner:
    """Accumulates phases and nodes while walking the entrypoint in source order.

    ``ctx.phase`` does not nest at run time: the runner sets ``_current_phase`` and
    its context manager's ``__exit__`` deliberately does NOT restore the previous
    phase, so a phase persists until the next call. This walker models exactly that
    — a phase opened inside a ``with`` block stays current after the block ends.
    """

    def __init__(self, ctx_name: str, local_functions: set[str]) -> None:
        self._ctx = ctx_name
        self._locals = local_functions
        self._phases: list[dict[str, Any]] = []
        self._current: Optional[dict[str, Any]] = None
        self._nodes = 0
        self.truncated = False

    # --- accumulation -------------------------------------------------------

    def _open_phase(self, title: str, *, certain: bool) -> Optional[dict[str, Any]]:
        """Open (or reuse) the phase named *title*, make it current, and return it.

        Reuse is by title because that is how the consumer pairs a plan phase with
        ``phase_started`` events — a script that re-enters a title produces one row
        there, so it must produce one row here. A re-entered phase is at best as
        certain as its least certain mention: one of the two may not run.

        The title is bounded to ``MAX_PHASE_TITLE_CHARS`` before anything retains or
        compares it, so the reuse test and the stored row always agree.
        """
        title = _bounded(title, MAX_PHASE_TITLE_CHARS)
        for existing in self._phases:
            if existing["title"] == title:
                if not certain:
                    existing["certain"] = False
                self._current = existing
                return existing
        if len(self._phases) >= MAX_PLAN_PHASES:
            self.truncated = True
            return None
        phase: dict[str, Any] = {"title": title, "certain": certain, "nodes": []}
        self._phases.append(phase)
        self._current = phase
        return phase

    def _add(self, kind: str, label: str, *, certain: bool, phase: Optional[str] = None) -> None:
        """Append one node, to *phase* when named explicitly, else to the current one."""
        if self._nodes >= MAX_PLAN_NODES:
            self.truncated = True
            return
        if phase is not None:
            # ``ctx.agent(..., phase="X")`` routes that one call elsewhere. Naming a
            # phase this way does NOT make it current for the statements that follow,
            # so the walker's position is restored afterwards.
            keep = self._current
            target = self._open_phase(phase, certain=certain)
            self._current = keep
        elif self._current is not None:
            target = self._current
        else:
            target = self._open_phase(_LEADING_PHASE, certain=True)
        if target is None:
            return  # phase cap reached; ``truncated`` already set
        target["nodes"].append({"kind": kind, "label": label, "certain": certain})
        self._nodes += 1

    # --- statements ---------------------------------------------------------

    def visit_body(self, body: list[ast.stmt], *, certain: bool) -> None:
        for stmt in body:
            if self.truncated:
                return
            self.visit(stmt, certain=certain)

    def visit(self, stmt: ast.stmt, *, certain: bool) -> None:
        for node_type, word in _BRANCHING_STMTS:
            if isinstance(stmt, node_type):
                self._add("unknown", word, certain=False)
                # The test / subject expressions are ordinary code and may hold calls.
                for expr in _branch_conditions(stmt):
                    self._visit_expr(expr, certain=False)
                for inner in _branch_bodies(stmt):
                    self.visit_body(inner, certain=False)
                return
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            self._visit_with(stmt, certain=certain)
            return
        if isinstance(stmt, ast.Expr):
            call = _unwrap(stmt.value)
            if _ctx_method(call, self._ctx) == "phase":
                assert isinstance(call, ast.Call)  # _ctx_method implies Call
                self._enter_phase(call, certain=certain)
                return
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A helper's body runs only where it is CALLED, and the call site is what
            # contributes a node, so the definition itself contributes nothing.
            return
        for child in ast.iter_child_nodes(stmt):
            self._visit_expr(child, certain=certain)

    def _enter_phase(self, call: ast.Call, *, certain: bool) -> bool:
        """Act on one ``ctx.phase(...)`` call. False when its title is not a literal.

        Both call shapes reach here — ``ctx.phase("x")`` bare and
        ``with ctx.phase("x"):`` — because the runner treats them identically: it
        sets ``_current_phase`` and the context manager restores nothing.

        A non-literal title means work certainly happens under a name only the run
        knows. Opening a phase for it would either collide with the leading unnamed
        phase or invent a title no ``phase_started`` event can be paired with, so the
        region is marked instead and its nodes stay where they are.
        """
        title = _literal_str(call.args[0]) if call.args else None
        if title is None:
            self._add("unknown", "phase", certain=False)
            return False
        self._open_phase(title, certain=certain)
        return True

    def _visit_with(self, stmt: Union[ast.With, ast.AsyncWith], *, certain: bool) -> None:
        body_certain = certain
        for item in stmt.items:
            call = _unwrap(item.context_expr)
            if _ctx_method(call, self._ctx) != "phase":
                self._visit_expr(item.context_expr, certain=certain)
                continue
            assert isinstance(call, ast.Call)  # _ctx_method returning a name implies Call
            if not self._enter_phase(call, certain=certain):
                body_certain = False
        self.visit_body(stmt.body, certain=body_certain)

    # --- expressions --------------------------------------------------------

    def _visit_expr(self, node: ast.AST, *, certain: bool) -> None:
        """Walk an expression tree, consuming the calls this module models.

        Hand-rolled rather than ``ast.walk`` because both pruning rules matter: a
        modelled call must not have its arguments re-counted (``ctx.parallel([...])``
        would otherwise emit its own nodes AND the inner ``ctx.agent`` calls again),
        and a lambda body must not be counted at its definition site.
        """
        if self.truncated:
            return
        if isinstance(node, ast.Lambda):
            # A lambda runs where it is invoked. ``ctx.parallel`` reads its thunks
            # itself; anywhere else, the call that invokes it is the node.
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        for node_type, word in _BRANCHING_EXPRS:
            if isinstance(node, node_type):
                self._add("unknown", word, certain=False)
                for child in ast.iter_child_nodes(node):
                    self._visit_expr(child, certain=False)
                return
        if isinstance(node, ast.Call) and self._visit_call(node, certain=certain):
            return
        for child in ast.iter_child_nodes(node):
            self._visit_expr(child, certain=certain)

    def _visit_call(self, call: ast.Call, *, certain: bool) -> bool:
        """Model *call* if it is one this module draws. True when fully consumed."""
        method = _ctx_method(call, self._ctx)
        if method == "agent":
            phase = None
            for kw in call.keywords:
                if kw.arg == "phase":
                    phase = _literal_str(kw.value)
            self._add("agent", _agent_label(call), certain=certain, phase=phase)
            return True
        if method == "parallel":
            self._visit_parallel(call, certain=certain)
            return True
        if method == "pipeline":
            # Width (items) times depth (stages), both decided at run time.
            self._add("unknown", "pipeline", certain=False)
            return True
        if method == "workflow":
            self._add("unknown", "nested", certain=False)
            return True
        if method is not None:
            if method not in NARRATION_CTX_METHODS and method not in MODELLED_CTX_METHODS:
                # A ctx method nothing here has classified. It may well spawn work, and a
                # call that spawns work while drawing nothing makes the plan quietly
                # wrong -- the worst of the three outcomes. Say "something happens here"
                # instead, and let the parity test name the method that needs deciding.
                self._add("unknown", "call", certain=False)
            # Otherwise: narration and side channels draw nothing by decision. Their
            # arguments are still ordinary code.
            return False
        if isinstance(call.func, ast.Name) and call.func.id in self._locals:
            self._add("unknown", "helper", certain=False)
            return False
        return False

    def _visit_parallel(self, call: ast.Call, *, certain: bool) -> None:
        tasks = call.args[0] if call.args else None
        if not isinstance(tasks, (ast.List, ast.Tuple)):
            # A name, a comprehension, or a call: the fan-out width is a runtime value.
            self._add("unknown", "parallel", certain=False)
            return
        for element in tasks.elts:
            inner = _unwrap(element)
            if isinstance(inner, ast.Lambda):
                inner = _unwrap(inner.body)
            if _ctx_method(inner, self._ctx) == "agent":
                assert isinstance(inner, ast.Call)  # _ctx_method implies Call
                self._add("agent", _agent_label(inner), certain=certain)
            else:
                # A task that is not literally an agent call — a helper thunk, a
                # nested parallel, anything. One node, no claim about its contents.
                self._add("unknown", "task", certain=False)

    def result(self) -> dict[str, Any]:
        return {
            "phases": self._phases,
            "truncated": self.truncated,
            "titleLimit": MAX_PHASE_TITLE_CHARS,
        }


def _branch_conditions(stmt: ast.stmt) -> list[ast.AST]:
    """The expressions a branching statement evaluates before choosing a body."""
    out: list[ast.AST] = []
    for attr in ("test", "iter", "subject"):
        expr = getattr(stmt, attr, None)
        if isinstance(expr, ast.AST):
            out.append(expr)
    return out


def plan_from_source(source: str) -> Optional[dict[str, Any]]:
    """Predicted phase/node plan for a workflow script, or None when there is none.

    None means "this source yields no plan" — it does not parse as Python, or it
    defines no ``workflow(ctx)`` entrypoint (a task-plan JSON source lands here).
    The UI must then show the actual run alone rather than an empty plan.
    """
    if not isinstance(source, str) or not source.strip():
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        # SyntaxError for bad syntax, ValueError for an embedded NUL, and deeply
        # nested input can exhaust the parser's stack. None of those is worth
        # failing a run snapshot over.
        return None
    entry = _find_entrypoint(tree)
    if entry is None:
        return None
    params = entry.args.args
    if not params:
        return None
    planner = _Planner(params[0].arg, _local_function_names(entry))
    planner.visit_body(entry.body, certain=True)
    return planner.result()
