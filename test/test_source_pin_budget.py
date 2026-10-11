"""Tests that read the dashboard turn's SOURCE: a per-module budget that may only fall.

A test that reads the runner's text -- ``inspect.getsource(_run_chat)``, a read of
``chat_runner.py``, an ``ast.parse`` of either -- tests past the turn's interface.
It breaks when the code it names moves, and stays green when the behaviour it
was written for regresses under the same spelling. A test of what a turn does
asserts on ``turn_harness.run_turn``'s ``TurnRecord`` instead. The runner's
remaining source reads are budgeted here, per test module, and the table must
equal what the tests read: a new read fails, and a change that removes a read
lowers its row in the same commit, so the room it freed cannot be spent again.

What counts as one read: a call of an ``inspect`` source reader
(``getsource``/``getsourcelines``/``getsourcefile``/``getfile``/``findsource``,
on the ``inspect`` module or imported from it under any name) on the runner
module, the ``chat_turn`` package it composes, or anything reached from them;
or a ``read_text``/``read_bytes``/``open`` of a path to one of their files
(:data:`_TURN_FILES`, matched on whole path segments). A location read
(``getsourcefile``) counts too: it pins where code lives, which is what a move
breaks. Names are followed through imports (a parent package's included),
``import_module``/``__import__``, ``getattr``, simple assignments, loop and
comprehension targets, class attributes read as ``self.X``/``cls.X``, a module's
``__file__`` and its ``.parent``, and ``/``-joined path pieces. An
``ast.parse`` of such a read is the same read and is not counted twice. The scan
does not follow a value handed in through a parameter or a fixture; a read routed
that way is still a read, and review is what catches it.

The pins kept, each with the reason no turn can stand in for it in its test's
docstring: the module-wide absence of a bare ``question_card`` emitter, the
bounded steer never re-wrapped at a call site, the clear arm's start-row reset (an
equivalent mutant), the runner's import boundary, every approval-mark site
flagging the slot dirty, and every verbatim recovery replay re-queuing the turn's
message without its merge cards.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterable
from pathlib import Path

import pytest
from source_corpus import repo_files_named, repo_root

pytestmark = pytest.mark.xdist_group(name="tree_scan_source_pin_budget")

#: The modules whose source a test must not read: the runner, and the owner
#: package its ``compose`` runs on the runner's globals.
_TURN_MODULES = ("kiro_crew.dashboard.chat_runner", "kiro_crew.dashboard.chat_turn")
_TURN_FILES = ("dashboard/chat_runner.py", "dashboard/chat_turn/")
_READERS = frozenset({"getsource", "getsourcelines", "getsourcefile", "getfile", "findsource"})
_TEXT_READERS = frozenset({"read_text", "read_bytes"})

#: Reads of the turn's source, per test module, at the last change that touched
#: this table. A module not listed may read none.
BUDGET: dict[str, int] = {
    # The turn-harness files: what stays, and why, is in each kept test's docstring.
    "test/metrics/test_turn_profile.py": 1,  # the runner's import boundary
    "test/test_dashboard_approval_window.py": 1,  # the bounded steer, never re-wrapped
    "test/test_file_change_snapshots.py": 1,  # the clear arm's start-row reset
    "test/test_native_question_card_lifecycle.py": 1,  # no bare question_card emitter
    "test/test_orphaned_approval_card.py": 1,  # every mark site flags the slot dirty
    # A kept call-site pin outside the turn-harness files; its reason is in its docstring.
    "test/test_chat_runner_coverage.py": 1,  # every verbatim replay drops the merge cards
    # Other scopes' reads, frozen as measured; each may only fall.
    "src/kiro_crew/apps/builtins/spec_builder/tests/test_routes.py": 1,
    "test/metrics/test_context_trace.py": 2,
    "test/metrics/test_usage_turns.py": 1,
    "test/test_autonudge_banner.py": 1,
    "test/test_channel_connect_row.py": 2,
    "test/test_chat_hooks.py": 1,
    "test/test_chat_runner_composition_contract.py": 5,
    "test/test_chat_runner_early_recovery_actor.py": 1,
    "test/test_chat_turn_timeout_consistency.py": 1,
    "test/test_credential_redaction_switch.py": 1,
    "test/test_credential_sources.py": 1,
    "test/test_crew_log_emit.py": 6,
    "test/test_crew_log_order.py": 3,
    "test/test_crew_log_previous_latch.py": 2,
    "test/test_dashboard_approval.py": 2,
    "test/test_dashboard_chat.py": 1,
    "test/test_deny_audit_first.py": 1,
    "test/test_decisions_compaction_hook.py": 1,
    "test/test_discord_empty_turn.py": 1,
    "test/test_external_logout_detection.py": 1,
    "test/test_gateway_appkit_endpoints.py": 1,
    "test/test_leaked_toolcall_notice.py": 5,
    "test/test_mcp_session_report.py": 1,
    "test/test_member_turn_context.py": 1,
    "test/test_name_grant_surfaces.py": 1,
    "test/test_no_config_dir_in_async.py": 1,
    "test/test_post_compaction_continuation.py": 3,
    "test/test_promise_only_autoapprove_parity.py": 1,
    "test/test_prompts.py": 2,
    "test/test_recovery_l1_chat_runner.py": 1,
    "test/test_redaction_allow.py": 1,
    "test/test_redaction_blocked_links.py": 5,
    "test/test_refusal_inband_notice.py": 2,
    "test/test_runloop_integration.py": 1,
    "test/test_runtime_death_is_a_process_event.py": 3,
    "test/test_session_control.py": 1,
    "test/test_session_control_owner_dm.py": 1,
    "test/test_session_control_set_model.py": 3,
    "test/test_start_priority.py": 1,
    "test/test_stderr_redact_then_tail.py": 1,
    "test/test_steer_requeue.py": 4,
    "test/test_subagent_stop_reason_consistency.py": 1,
    "test/test_turn_duration_recorded.py": 1,
    "test/test_turn_metric_all_surfaces.py": 2,
    "test/test_wake_judge_feedback.py": 1,
    "test/test_ws_event_scoping.py": 1,
}


def _names_a_turn_module(dotted: str) -> bool:
    return any(dotted == module or dotted.startswith(module + ".") for module in _TURN_MODULES)


def _leads_to_a_turn_module(dotted: str) -> bool:
    """*dotted* is a turn module, inside one, or a package one sits in."""
    return _names_a_turn_module(dotted) or any(
        module.startswith(dotted + ".") for module in _TURN_MODULES
    )


def _names_a_turn_file(text: str) -> bool:
    """*text* is a path to one of the :data:`_TURN_FILES`, matched on whole
    path segments: ``test_chat_runner.py`` or ``telegram/chat_runner.py`` is not."""
    padded = "/" + text.replace("\\", "/").strip("/") + "/"
    return any("/" + turn_file.strip("/") + "/" in padded for turn_file in _TURN_FILES)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
#: How many spellings of one path the scan keeps, so loops over long tables of
#: names cannot grow the work without bound.
_MAX_TEXTS = 64
#: The names a method reads its class's attributes through.
_SELVES = frozenset({"self", "cls"})


def _own_nodes(scope: ast.AST) -> Iterable[ast.AST]:
    """Every node of *scope* outside the functions nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _parameters(function: ast.AST) -> set[str]:
    args = getattr(function, "args", None)
    if args is None:
        return set()
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {arg.arg for arg in every if arg is not None}


def _assignments(node: ast.AST) -> Iterable[tuple[ast.AST, ast.AST, bool]]:
    """``(target, value, iterated)`` for each binding *node* makes."""
    if isinstance(node, ast.Assign):
        return ((target, node.value, False) for target in node.targets)
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
        return ((node.target, node.value, False),)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return ((node.target, node.iter, True),)
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return ((node.optional_vars, node.context_expr, False),)
    return ()


def _self_attribute(expr: ast.AST) -> str | None:
    """``X`` for ``self.X`` / ``cls.X``: a class attribute read from a method."""
    if (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id in _SELVES
    ):
        return expr.attr
    return None


class _Scan:
    """One scope's view of which names reach the turn's source.

    A class body is part of its enclosing scope here, so a class attribute
    bound to the runner or one of its paths is seen when a method reads it as
    ``self.X`` / ``cls.X``.
    """

    def __init__(
        self,
        bound: set[str],
        aliases: dict[str, str],
        paths: set[str],
        texts: dict[str, set[str]],
        readers: set[str],
        inspects: set[str],
    ) -> None:
        self.bound = bound  # names bound to a turn module or anything in one
        self.aliases = aliases  # import names -> the dotted module they stand for
        self.paths = paths  # names bound to a path of one of the turn's files
        self.texts = texts  # names bound to any path: the texts it can spell
        self.readers = readers  # names an ``inspect`` source reader is bound to
        self.inspects = inspects  # names the ``inspect`` module is bound to

    def inner(self, shadowed: set[str]) -> "_Scan":
        """The view inside a function whose parameters are *shadowed*."""
        return _Scan(
            self.bound - shadowed,
            {name: dotted for name, dotted in self.aliases.items() if name not in shadowed},
            self.paths - shadowed,
            {name: texts for name, texts in self.texts.items() if name not in shadowed},
            self.readers - shadowed,
            self.inspects - shadowed,
        )

    def imports_a_turn_module(self, call: ast.Call) -> bool:
        """``import_module("<turn module>")`` (or an f-string led by a turn
        module's ``__name__``), or ``__import__`` of one with a ``fromlist``
        (without one it returns the top-level package)."""
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("import_module", "__import__") or not call.args:
            return False
        if name == "__import__" and len(call.args) < 4:
            if not any(keyword.arg == "fromlist" for keyword in call.keywords):
                return False
        target = call.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            return _names_a_turn_module(target.value)
        if isinstance(target, ast.JoinedStr) and target.values:
            lead = target.values[0]
            if isinstance(lead, ast.Constant) and isinstance(lead.value, str):
                return _names_a_turn_module(lead.value.rstrip("."))
            if isinstance(lead, ast.FormattedValue):
                value = lead.value
                return (
                    isinstance(value, ast.Attribute)
                    and value.attr == "__name__"
                    and self.rooted(value.value)
                )
        return False

    def rooted(self, expr: ast.AST) -> bool:
        """*expr* is a turn module, or something reached from one."""
        attrs: list[str] = []
        chain = expr
        while isinstance(chain, (ast.Attribute, ast.Call)):
            if isinstance(chain, ast.Call):
                if self.imports_a_turn_module(chain):
                    return True
                func = chain.func
                if isinstance(func, ast.Name) and func.id == "getattr" and chain.args:
                    owner = chain.args[0]
                    named = chain.args[1] if len(chain.args) > 1 else None
                    if isinstance(named, ast.Constant) and isinstance(named.value, str):
                        owner = ast.Attribute(value=owner, attr=named.value)
                    return self.rooted(owner)
                attrs = []
                chain = func
                continue
            if _self_attribute(chain) in self.bound:
                return True
            attrs.append(chain.attr)
            chain = chain.value
        if isinstance(chain, ast.Name):
            if chain.id in self.bound:
                return True
            if chain.id in self.aliases:
                return _names_a_turn_module(".".join([self.aliases[chain.id], *reversed(attrs)]))
            return False
        if isinstance(chain, ast.Subscript) and isinstance(chain.slice, ast.Constant):
            return isinstance(chain.slice.value, str) and _names_a_turn_module(chain.slice.value)
        return False

    def dotted(self, expr: ast.AST) -> str | None:
        """The module *expr* names through an import, as a dotted name."""
        attrs: list[str] = []
        while isinstance(expr, ast.Attribute):
            attrs.append(expr.attr)
            expr = expr.value
        if isinstance(expr, ast.Name) and expr.id in self.aliases:
            return ".".join([self.aliases[expr.id], *reversed(attrs)])
        return None

    def path_text(self, expr: ast.AST) -> set[str]:
        """Every path *expr* can spell: ``/``-joined pieces, named paths, a
        module's ``__file__`` and its ``.parent`` substituted. Empty if none."""
        texts: set[str] = set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            left, right = self.path_text(expr.left), self.path_text(expr.right)
            texts = {f"{a}/{b}" for a in left for b in right} or left or right
        elif isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            texts = {expr.value}
        elif isinstance(expr, ast.Name):
            texts = self.texts.get(expr.id, set())
        elif isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            texts = {text for element in expr.elts for text in self.path_text(element)}
        elif isinstance(expr, ast.Attribute):
            attribute = _self_attribute(expr)
            if attribute is not None:
                texts = self.texts.get(attribute, set())
            elif expr.attr == "__file__":
                module = self.dotted(expr.value)
                if module is not None:
                    stem = module.replace(".", "/")
                    texts = {f"{stem}.py", f"{stem}/__init__.py"}
            elif expr.attr == "parent":
                texts = {
                    text.rsplit("/", 1)[0] for text in self.path_text(expr.value) if "/" in text
                }
        elif isinstance(expr, ast.Call) and not expr.keywords:
            func = expr.func
            if len(expr.args) == 1:
                texts = self.path_text(expr.args[0])  # Path("...") and its kin
            elif not expr.args and isinstance(func, ast.Attribute):
                if func.attr in ("resolve", "absolute"):
                    texts = self.path_text(func.value)
        return set(sorted(texts)[:_MAX_TEXTS])

    def a_path(self, expr: ast.AST) -> bool:
        """*expr* is (or is built from) a path to one of the turn's files."""
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Attribute) and sub.attr == "__file__":
                if self.rooted(sub.value):
                    return True
            elif isinstance(sub, ast.Name) and sub.id in self.paths:
                return True
            elif _self_attribute(sub) in self.paths:
                return True
            elif isinstance(sub, (ast.Constant, ast.BinOp, ast.Attribute)):
                if any(_names_a_turn_file(text) for text in self.path_text(sub)):
                    return True
        return False

    def a_reader(self, expr: ast.AST) -> bool:
        """``inspect.getsource`` (or another reader), or a name bound to one."""
        if isinstance(expr, ast.Attribute):
            return (
                expr.attr in _READERS
                and isinstance(expr.value, ast.Name)
                and expr.value.id in self.inspects
            )
        return isinstance(expr, ast.Name) and expr.id in self.readers


def _imports(nodes: Iterable[ast.AST], scan: _Scan) -> None:
    """Fold the import lines among *nodes* into *scan*."""
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "inspect":
                    if alias.name == "*":
                        scan.readers |= _READERS
                    elif alias.name in _READERS:
                        scan.readers.add(local)
                    else:
                        scan.readers.discard(local)
                    continue
                full = f"{node.module}.{alias.name}"
                if _leads_to_a_turn_module(full):
                    scan.aliases[local] = full
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "inspect":
                    scan.inspects.add(alias.asname or "inspect")
                elif alias.asname and _leads_to_a_turn_module(alias.name):
                    scan.aliases[alias.asname] = alias.name
                elif not alias.asname and _leads_to_a_turn_module(alias.name):
                    top = alias.name.split(".")[0]
                    scan.aliases[top] = top


def _scan_scope(scope: ast.AST, scan: _Scan, reads: list[tuple[int, str]]) -> None:
    own = list(_own_nodes(scope))
    _imports(own, scan)
    bindings = [binding for node in own for binding in _assignments(node)]
    changed = True
    while changed:
        changed = False
        for target, value, iterated in bindings:
            names = {sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name)}
            elements = value.elts if iterated and isinstance(value, (ast.Tuple, ast.List)) else []
            # A name's first path binding wins: ``path = path / "x"`` must not
            # grow its texts on every pass.
            fresh = [name for name in names if name not in scan.texts]
            texts = scan.path_text(value) if fresh else set()
            if texts:
                scan.texts.update(dict.fromkeys(fresh, texts))
                changed = True
            if isinstance(value, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
                value = value.elt  # what the comprehension's items are
            if scan.a_reader(value):
                new = names - scan.readers
                scan.readers |= new
            elif scan.rooted(value) or any(scan.rooted(element) for element in elements):
                new = names - scan.bound
                scan.bound |= new
            elif scan.a_path(value):
                new = names - scan.paths
                scan.paths |= new
            else:
                continue
            changed = changed or bool(new)
    for node in own:
        if isinstance(node, _SCOPES):
            _scan_scope(node, scan.inner(_parameters(node)), reads)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if scan.a_reader(func) and node.args and scan.rooted(node.args[0]):
            reads.append((node.lineno, "getsource"))
        elif name in _TEXT_READERS and isinstance(func, ast.Attribute) and scan.a_path(func.value):
            reads.append((node.lineno, "source_text"))
        elif name == "open" and (
            (isinstance(func, ast.Attribute) and scan.a_path(func.value))
            or (node.args and scan.a_path(node.args[0]))
        ):
            reads.append((node.lineno, "source_text"))


def turn_source_reads(source: str) -> list[tuple[int, str]]:
    """``(line, kind)`` of each read of the turn's source in a module's *source*.

    Scope-aware: a name bound to the runner inside one function says nothing
    about the same name in another, and a parameter shadows an outer binding.
    """
    reads: list[tuple[int, str]] = []
    scan = _Scan(set(), {}, set(), {}, set(_READERS), set())
    _scan_scope(ast.parse(source), scan, reads)
    return sorted(reads)


def _is_a_test_module(rel: Path) -> bool:
    parts = rel.parts
    return bool(parts) and (
        parts[0] == "test"
        or (
            len(parts) > 5
            and parts[:4] == ("src", "kiro_crew", "apps", "builtins")
            and "tests" in parts
        )
    )


@functools.lru_cache(maxsize=1)
def _measured() -> dict[str, int]:
    """Reads per test module, over every test module the checkout holds."""
    root = repo_root()
    counts: dict[str, int] = {}
    for path in repo_files_named(".py"):
        rel = path.relative_to(root)
        if not _is_a_test_module(rel) or rel.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # A module that never names the runner or its owners cannot reach them.
        if "chat_runner" not in text and "chat_turn" not in text:
            continue
        try:
            reads = turn_source_reads(text)
        except SyntaxError:
            continue
        if reads:
            counts[rel.as_posix()] = len(reads)
    return counts


def test_no_test_module_reads_more_of_the_turns_source_than_its_budget() -> None:
    over = {
        module: (count, BUDGET.get(module, 0))
        for module, count in sorted(_measured().items())
        if count > BUDGET.get(module, 0)
    }
    assert over == {}, (
        "these test modules read the dashboard turn's source past their budget "
        "(reads, budget). Assert on what a turn does instead -- "
        "test/turn_harness.py's run_turn returns it as a TurnRecord: "
        f"{over}"
    )


def test_a_budget_falls_with_the_reads_it_covers() -> None:
    """A module that dropped a read lowers its entry, so the room cannot be spent
    again; an entry for a module that reads nothing is removed. This also keeps
    the scan honest: one that silently found nothing would fail every entry."""
    measured = _measured()
    stale = {
        module: (measured.get(module, 0), budget)
        for module, budget in sorted(BUDGET.items())
        if measured.get(module, 0) < budget or budget == 0
    }
    assert stale == {}, (
        "these BUDGET entries are above what the module reads (reads, budget): "
        f"lower each to its reads, or delete it at 0: {stale}"
    )


# ── the scanner, on the forms it must and must not count ────────────────────

_COUNTED = [
    (
        "from kiro_crew.dashboard import chat_runner\n"
        "import inspect\n"
        "src = inspect.getsource(chat_runner._run_chat)\n",
        1,
    ),
    (
        "import inspect as _inspect\n"
        "from kiro_crew.dashboard import chat_runner as cr\n"
        "mod = cr\n"
        "_inspect.getsource(mod._steer_policy_notice)\n",
        1,
    ),
    (
        "from kiro_crew.dashboard.chat_runner import _run_chat\n"
        "import ast, inspect\n"
        "ast.parse(inspect.getsource(_run_chat))\n",
        1,
    ),
    (
        "import inspect\nimport kiro_crew.dashboard.chat_runner as runner\n"
        "inspect.getsource(runner)\n",
        1,
    ),
    (
        "import inspect, sys\n"
        "inspect.getsource(sys.modules['kiro_crew.dashboard.chat_runner'])\n",
        1,
    ),
    (
        "from pathlib import Path\n"
        "from kiro_crew.dashboard import chat_runner\n"
        "Path(chat_runner.__file__).read_text()\n",
        1,
    ),
    (
        "from pathlib import Path\n"
        "RUNNER = Path(__file__).parents[1] / 'src/kiro_crew/dashboard/chat_runner.py'\n"
        "text = RUNNER.read_text(encoding='utf-8')\n",
        1,
    ),
    (
        "from pathlib import Path\n"
        "from kiro_crew.dashboard import chat_runner, chat_turn\n"
        "sources = [Path(chat_runner.__file__)] + sorted(Path(chat_turn.__file__).parent.glob('*.py'))\n"
        "found = [p for path in sources for p in path.read_text().split()]\n",
        1,
    ),
    (
        "import inspect\n"
        "from kiro_crew.dashboard.chat_turn import tool_approval\n"
        "inspect.getsource(tool_approval)\n",
        1,
    ),
    (
        "import inspect\nfrom kiro_crew.dashboard import chat_runner, chat_handlers\n"
        "for module in (chat_runner, chat_handlers):\n"
        "    inspect.getsource(module)\n",
        1,
    ),
    ("with open('src/kiro_crew/dashboard/chat_runner.py') as fh:\n    fh.read()\n", 1),
    (
        "from pathlib import Path\nfrom kiro_crew.dashboard import chat_runner\n"
        "with Path(chat_runner.__file__).open() as fh:\n    fh.read()\n",
        1,
    ),
    (
        "import io\nio.open('src/kiro_crew/dashboard/chat_runner.py').read()\n",
        1,
    ),
    (
        "from inspect import getsource as gs\nfrom kiro_crew.dashboard import chat_runner\n"
        "gs(chat_runner._run_chat)\n",
        1,
    ),
    (
        "import inspect\nfrom kiro_crew.dashboard import chat_runner\n"
        "inspect.getsource(getattr(chat_runner, '_run_chat'))\n",
        1,
    ),
    (
        "import importlib, inspect\n"
        "inspect.getsource(importlib.import_module('kiro_crew.dashboard.chat_runner')._run_chat)\n",
        1,
    ),
    (
        "import inspect\n"
        "runner = __import__('kiro_crew.dashboard.chat_runner', fromlist=['x'])\n"
        "inspect.getsourcelines(runner)\n",
        1,
    ),
    (
        "import inspect\nfrom kiro_crew.dashboard import chat_runner\n"
        "read_source = inspect.getsource\nread_source(chat_runner._run_chat)\n",
        1,
    ),
    (
        "from pathlib import Path\nclass TestX:\n"
        "    RUNNER = Path(__file__).parents[1] / 'src/kiro_crew/dashboard/chat_runner.py'\n"
        "    def test_it(self):\n        self.RUNNER.read_text()\n",
        1,
    ),
    (
        "import inspect\nfrom kiro_crew import dashboard\n"
        "inspect.getsource(dashboard.chat_runner._run_chat)\n",
        1,
    ),
    ("import inspect\nimport kiro_crew.dashboard as d\ninspect.getsource(d.chat_runner)\n", 1),
    (
        "import importlib, pkgutil\nfrom pathlib import Path\n"
        "from kiro_crew.dashboard import chat_turn\n"
        "owners = [importlib.import_module(f'{chat_turn.__name__}.{i.name}')\n"
        "          for i in pkgutil.iter_modules(chat_turn.__path__)]\n"
        "texts = [Path(m.__file__).read_text() for m in owners]\n",
        1,
    ),
    (
        "from pathlib import Path\nimport kiro_crew.dashboard as pkg\n"
        "root = Path(pkg.__file__).parent\n"
        "for name in ('state.py', 'chat_runner.py'):\n    (root / name).read_text()\n",
        1,
    ),
    (
        "from inspect import *\nfrom kiro_crew.dashboard import chat_runner\n"
        "getsource(chat_runner._run_chat)\n",
        1,
    ),
    (
        "from pathlib import Path\nROOT = Path(__file__).parent\n"
        "pkg = ROOT / 'src' / 'kiro_crew' / 'dashboard' / 'chat_turn'\n"
        "for path in sorted(pkg.glob('*.py')):\n    path.read_text()\n",
        1,
    ),
]

_NOT_COUNTED = [
    "import inspect\nfrom kiro_crew.dashboard import chat_handlers\n"
    "inspect.getsource(chat_handlers.api_chat)\n",
    "import inspect, sys\ninspect.getsource(sys.modules[__name__])\n",
    "from pathlib import Path\nPath('docs/system-specs/modules/learn-cron-dashboard.md').read_text()\n",
    "import inspect\nfrom kiro_crew.messaging import identity\n"
    "inspect.getsource(identity.publish_turn_identity)\n",
    "from kiro_crew.dashboard import chat_runner\nchat_runner._run_chat.__wrapped__\n",
    "import inspect\nfrom kiro_crew.dashboard import chat_runner\n"
    "inspect.signature(chat_runner._run_chat)\n",
    "PATHS = ['src/kiro_crew/dashboard/chat_runner.py']\nprint(PATHS)\n",
    "import kiro_crew.config\nimport inspect\ninspect.getsource(kiro_crew.config)\n",
    "from inspect import signature as getsource\nfrom kiro_crew.dashboard import chat_runner\n"
    "getsource(chat_runner._run_chat)\n",
    "import importlib, inspect\n"
    "inspect.getsource(importlib.import_module('kiro_crew.dashboard.chat_handlers'))\n",
    "from kiro_crew.dashboard import chat_runner\nopen('notes.txt').read()\n",
    "import inspect\ninspect.getsource(__import__('kiro_crew.dashboard.chat_runner'))\n",
    "from pathlib import Path\nPath('test/test_chat_runner.py').read_text()\n",
    "from pathlib import Path\nPath('src/kiro_crew/telegram/chat_runner.py').read_text()\n",
    "import mylib\nfrom kiro_crew.dashboard import chat_runner\nmylib.getsource(chat_runner._run_chat)\n",
    "import inspect\nfrom kiro_crew import dashboard\ninspect.getsource(dashboard.chat_handlers)\n",
    "from pathlib import Path\nimport kiro_crew.dashboard as pkg\n"
    "(Path(pkg.__file__).parent / 'state.py').read_text()\n",
    "from pathlib import Path\nclass TestX:\n"
    "    OTHER = Path(__file__).parents[1] / 'src/kiro_crew/dashboard/chat_handlers.py'\n"
    "    def test_it(self):\n        self.OTHER.read_text()\n",
    "import importlib\nfrom pathlib import Path\nfrom kiro_crew.dashboard import chat_handlers\n"
    "module = importlib.import_module(f'{chat_handlers.__name__}')\n"
    "Path(module.__file__).read_text()\n",
    "from pathlib import Path\nROOT = Path(__file__).parent\n"
    "(ROOT / 'src' / 'kiro_crew' / 'dashboard' / 'chat_handlers.py').read_text()\n",
]


@pytest.mark.parametrize(("source", "reads"), _COUNTED, ids=range(len(_COUNTED)))
def test_the_scanner_counts_a_read_of_the_turns_source(source: str, reads: int) -> None:
    assert len(turn_source_reads(source)) == reads


@pytest.mark.parametrize("source", _NOT_COUNTED, ids=range(len(_NOT_COUNTED)))
def test_the_scanner_ignores_what_is_not_such_a_read(source: str) -> None:
    assert turn_source_reads(source) == []
