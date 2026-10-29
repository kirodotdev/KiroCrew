"""Pin every site that passes or declares ``_promote_from_ephemeral``.

``fork_slot(..., _promote_from_ephemeral=True)`` forces the child's
``memory_mode`` to ``persistent`` through ``ExecutionContext.promoted_to_persistent``,
the one method that loosens a record's mode (``with_mode`` only tightens), so the
flag loosens privacy. Only the dashboard's promote route may set it:
``api_chat_slot_promote`` passes the constant, ``api_chat_slot_fork`` forwards
its own parameter into ``fork_slot``. Any other caller, including the
agent-reachable ``session_fork`` path in ``session_control.py``, must not be
able to pass it, and this scan of ``src/kiro_crew`` is what enforces that.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest
from source_corpus import parsed_candidates, src_root

FLAG = "_promote_from_ephemeral"

# (module path relative to the package, enclosing function, passed value).
EXPECTED_CALL_SITES = {
    ("dashboard/chat_fork.py", "api_chat_slot_promote", "True"),
    ("dashboard/chat_fork.py", "api_chat_slot_fork", FLAG),
}

# (module path relative to the package, function declaring the parameter).
EXPECTED_DEFINITIONS = {
    ("dashboard/chat_fork.py", "api_chat_slot_fork"),
    ("dashboard/chat_fork.py", "fork_slot"),
}
EXPECTED_DEFINITION_NAMES = {name for _module, name in EXPECTED_DEFINITIONS}

_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _enclosing_functions(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the name of its innermost enclosing function."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        if isinstance(node, _FunctionDef):
            current = node.name
        owner[node] = current
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree, "<module>")
    return owner


def _passed_value(keyword: ast.keyword) -> str:
    """Render a keyword's value so a constant and a forwarded name compare exactly."""
    return ast.unparse(keyword.value)


@dataclass(frozen=True)
class _Scan:
    call_sites: frozenset[tuple[str, str, str]]
    definitions: frozenset[tuple[str, str]]
    files_scanned: int


def _scan() -> _Scan:
    """Collect call sites passing the flag, definitions declaring it, and files scanned."""
    root = src_root()
    call_sites: set[tuple[str, str, str]] = set()
    definitions: set[tuple[str, str]] = set()
    files_scanned = 0
    for path, _text, tree in parsed_candidates(require_all=(FLAG,), skip_syntax_errors=False):
        files_scanned += 1
        module = Path(path).resolve().relative_to(root).as_posix()
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == FLAG:
                        call_sites.add((module, owner[node], _passed_value(keyword)))
            elif isinstance(node, _FunctionDef):
                params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if any(param.arg == FLAG for param in params):
                    definitions.add((module, node.name))
    return _Scan(frozenset(call_sites), frozenset(definitions), files_scanned)


class TestPromoteFlagCallSites:
    @pytest.fixture(scope="class")
    def scan(self) -> _Scan:
        return _scan()

    def test_scan_finds_the_flag(self, scan: _Scan) -> None:
        assert scan.files_scanned >= 1, "scan found no file mentioning the flag"
        assert scan.call_sites, "scan found no call passing the flag"
        assert scan.definitions, "scan found no function declaring the flag"

    def test_only_the_promote_route_passes_the_flag(self, scan: _Scan) -> None:
        extra = scan.call_sites - EXPECTED_CALL_SITES
        assert not extra, f"unexpected caller(s) pass {FLAG}: {sorted(extra)}"
        assert scan.call_sites == EXPECTED_CALL_SITES

    def test_only_fork_slot_and_its_route_declare_the_flag(self, scan: _Scan) -> None:
        extra = scan.definitions - EXPECTED_DEFINITIONS
        assert not extra, f"unexpected function(s) declare {FLAG}: {sorted(extra)}"
        assert scan.definitions == EXPECTED_DEFINITIONS

    def test_fork_slot_defaults_the_flag_to_false(self) -> None:
        chat_fork = src_root() / "dashboard" / "chat_fork.py"
        tree = ast.parse(chat_fork.read_text(encoding="utf-8"), filename=str(chat_fork))
        defaults: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, _FunctionDef) or node.name not in EXPECTED_DEFINITION_NAMES:
                continue
            kwonly = node.args.kwonlyargs
            for param, default in zip(kwonly, node.args.kw_defaults, strict=True):
                if param.arg == FLAG:
                    assert default is not None, f"{node.name}: {FLAG} must have a default"
                    defaults[node.name] = ast.unparse(default)
        assert defaults == {"fork_slot": "False", "api_chat_slot_fork": "False"}


LOOSEN = "promoted_to_persistent"


class TestPromotedToPersistentCallSites:
    """``promoted_to_persistent`` is the only way to loosen a mode; pin who may call it."""

    def test_only_the_fork_bind_helper_loosens_the_mode(self) -> None:
        root = src_root()
        callers: set[tuple[str, str]] = set()
        for path, _text, tree in parsed_candidates(require_all=(LOOSEN,), skip_syntax_errors=False):
            module = Path(path).resolve().relative_to(root).as_posix()
            owner = _enclosing_functions(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == LOOSEN
                ):
                    callers.add((module, owner[node]))
        assert callers == {("dashboard/chat_fork.py", "_bind_fork_execution")}

    def test_the_bind_helper_loosens_only_under_promote_and_fork_slot_passes_the_flag(self) -> None:
        chat_fork = src_root() / "dashboard" / "chat_fork.py"
        tree = ast.parse(chat_fork.read_text(encoding="utf-8"), filename=str(chat_fork))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, _FunctionDef) and node.name == "_bind_fork_execution":
                guarded = [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.If)
                    and ast.unparse(n.test) == "promote"
                    and any(
                        isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Attribute)
                        and c.func.attr == LOOSEN
                        for b in n.body
                        for c in ast.walk(b)
                    )
                ]
                assert len(guarded) == 1
        promoted_binds = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "partial"
            and [ast.unparse(a) for a in node.args] == ["_bind_fork_execution"]
            and [(k.arg, ast.unparse(k.value)) for k in node.keywords] == [("promote", "True")]
            and owner[node] == "fork_slot"
        ]
        assert len(promoted_binds) == 1
        guards = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and owner[node] == "fork_slot"
            and ast.unparse(node.test) == FLAG
            and any(c is promoted_binds[0] for b in node.body for c in ast.walk(b))
        ]
        assert len(guards) == 1
