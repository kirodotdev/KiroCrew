"""GATE -- no strict text read of ``/proc/<pid>/stat``, ``status`` or ``comm``.

## The failure class

A process's name (``comm``) is arbitrary bytes. Any process may set it with
``prctl(PR_SET_NAME)``, and the kernel cuts an ordinary long name at 15 bytes,
mid-character when it is multibyte: ``run_データ処理.py`` becomes a name that is
not valid UTF-8. ``stat`` and ``comm`` carry it raw, and so does ``status``'s
``Name:`` line. A strict text read of any of the three raises
``UnicodeDecodeError`` for that one process -- which an ``except OSError`` does
not catch, and which an ``except ValueError`` catches as "gone". On a kill path
either one is a silent leak: a check raises out of the sweep, or a live member
of a group reads as absent and the group is declared empty.

## The rule

Inside ``src/kiro_crew`` (vendored code and in-package test suites excluded), a
read whose path ends in ``stat``, ``status`` or ``comm`` must not decode
strictly. Flagged:

* ``<path>.read_text(...)`` without ``errors=``;
* ``open(<path>)`` / ``io.open`` / ``<path>.open()`` in a text mode without
  ``errors=``;
* ``<path>.read_bytes().decode(...)`` without an error handler.

``errors="strict"`` counts as no handler. The path is followed through a ``/``
join, a one-argument ``Path(...)`` / ``str(...)`` / ``os.fspath(...)``, an
f-string, a conditional expression and a name assigned in the same function, an
enclosing one or at module level. Only files whose text holds a leaf as
``/stat"`` (the end of a path literal) or ``/ "stat"`` (a join) are parsed, which
keeps the scan to a few dozen files. Out of reach: a leaf spelled any other way --
``os.path.join(d, "stat")``, ``Path(d, "stat")`` -- of which none exists in the
tree, and a leaf that reaches the read as a function argument, which the scan
cannot follow across the call (``doctor_checks/confinement.py`` reads
``/proc/self/<name>`` that way, decoding with ``errors="replace"``).

The fix, in order of preference: ``platform_compat.read_proc_stat`` for a
``stat`` field and ``read_proc_status_int`` for a ``status`` number; a bytes
read decoded with ``surrogateescape`` or ``replace`` for ``comm`` (as
``platform_compat.linux_process_name`` does); ``errors="replace"`` for a
``status`` read that needs more, whose numeric lines are ASCII. The host-wide
``/proc/stat`` names no process and is not policed.

## No exceptions

There is no exception list. ``_process_group_supervisor.py`` runs as
``python -I -c`` and so cannot import ``platform_compat``, but its local parse
reads bytes and passes this gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from source_corpus import iter_source_texts, src_root

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_proc_bytes_reads_gate")

#: The ``/proc/<pid>/`` leaves whose content can carry a raw ``comm``.
PROC_LEAVES = frozenset({"stat", "status", "comm"})

_STRICT = (None, "strict")

#: The source spellings a flagged path can end in: a literal ending ``/<leaf>``
#: (``f"/proc/{pid}/stat"``), or a join onto a ``"<leaf>"`` literal, formatted or not.
_LEAF_SPELLINGS = tuple(
    spelling
    for leaf in sorted(PROC_LEAVES)
    for quote in "\"'"
    for spelling in (f"/{leaf}{quote}", f"/ {quote}{leaf}{quote}", f"/{quote}{leaf}{quote}")
)


def _errors_value(call: ast.Call, positional_index: int | None) -> tuple[bool, object]:
    """``(present, literal value or Ellipsis when not a literal)`` for the call's ``errors``."""
    for kw in call.keywords:
        if kw.arg == "errors":
            return True, kw.value.value if isinstance(kw.value, ast.Constant) else ...
        if kw.arg is None:  # a ``**kwargs`` splat may carry it: not judged
            return True, ...
    if positional_index is not None and len(call.args) > positional_index:
        node = call.args[positional_index]
        return True, node.value if isinstance(node, ast.Constant) else ...
    return False, None


def _has_handler(call: ast.Call, positional_index: int | None) -> bool:
    present, value = _errors_value(call, positional_index)
    return present and value not in _STRICT


def _own_nodes(body: ast.AST) -> list[ast.AST]:
    """*body*'s nodes, not descending into a nested function, lambda or class."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(body))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            pending.extend(ast.iter_child_nodes(node))
    return found


class _Scope:
    """The names one function body (or the module) assigns, for path following.

    A name the body does not assign resolves in *parent*: the enclosing
    function, then the module, so a path held in a module-level constant is
    followed into every function that reads it. ``whole`` takes every
    assignment in the file instead, the superset the cheap pre-pass needs.
    """

    def __init__(
        self, body: ast.AST, parent: "_Scope | None" = None, *, whole: bool = False
    ) -> None:
        self.parent = parent
        self.assigned: dict[str, list[ast.expr]] = {}
        for node in ast.walk(body) if whole else _own_nodes(body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assigned.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self.assigned.setdefault(node.target.id, []).append(node.value)


def _leaves(expr: ast.expr, scope: _Scope, depth: int = 0) -> set[str]:
    """The possible final path components *expr* evaluates to, as far as they are literal."""
    if depth > 8:
        return set()
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        parts = expr.value.rstrip("/").split("/")
        if parts[-2:-1] == ["proc"]:
            return set()  # the host-wide /proc/stat, which names no process
        return {parts[-1]}
    if isinstance(expr, ast.JoinedStr):
        last = expr.values[-1] if expr.values else None
        if isinstance(last, ast.Constant) and isinstance(last.value, str) and "/" in last.value:
            return {last.value.rsplit("/", 1)[-1]}
        return set()
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return _leaves(expr.right, scope, depth + 1)
    if isinstance(expr, ast.IfExp):
        return _leaves(expr.body, scope, depth + 1) | _leaves(expr.orelse, scope, depth + 1)
    if isinstance(expr, ast.Call) and len(expr.args) == 1:
        func = expr.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in {"Path", "PurePath", "PosixPath", "str", "fspath"}:
            return _leaves(expr.args[0], scope, depth + 1)
        return set()
    if isinstance(expr, ast.Name):
        owner: _Scope | None = scope
        while owner is not None and expr.id not in owner.assigned:
            owner = owner.parent
        found: set[str] = set()
        for value in owner.assigned[expr.id] if owner is not None else ():
            found |= _leaves(value, owner, depth + 1)
        return found
    return set()


def _strict_read_path(call: ast.Call) -> ast.expr | None:
    """The path expression of *call* when it is a strict text read, else None."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "read_text":
        return None if _has_handler(call, 1) else func.value
    if isinstance(func, ast.Attribute) and func.attr == "decode":
        inner = func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "read_bytes"
            and not _has_handler(call, 1)
        ):
            return inner.func.value
        return None
    builtin_open = (isinstance(func, ast.Name) and func.id == "open") or (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id == "io"
    )
    path_open = (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and not (isinstance(func.value, ast.Name) and func.value.id in {"os", "io"})
    )
    if not (builtin_open or path_open):
        return None
    mode_index = 1 if builtin_open else 0
    mode: ast.expr | None = next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
    if mode is None and len(call.args) > mode_index:
        mode = call.args[mode_index]
    if mode is not None:
        if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            return None  # a computed mode is not judged
        if "b" in mode.value:
            return None
    # ``open(file, mode, buffering, encoding, errors)``; ``Path.open`` has no ``file``.
    if _has_handler(call, 4 if builtin_open else 3):
        return None
    if builtin_open:
        return call.args[0] if call.args else None
    return func.value


def find_violations(source: str) -> list[tuple[int, str]]:
    """``(line, function)`` for every strict ``/proc`` leaf read in *source*."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    # One flat pass first, resolving names against EVERY assignment in the file
    # (a superset of any one function's): a file with no possible hit -- nearly
    # all of them -- skips the scoped walk below, which costs several times more.
    whole_file = _Scope(tree, whole=True)
    if not any(
        isinstance(node, ast.Call)
        and (path := _strict_read_path(node)) is not None
        and _leaves(path, whole_file) & PROC_LEAVES
        for node in ast.walk(tree)
    ):
        return found

    def visit(node: ast.AST, scope: _Scope, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, _Scope(child, scope), child.name)
                continue
            if isinstance(child, ast.Call):
                path = _strict_read_path(child)
                if path is not None and _leaves(path, scope) & PROC_LEAVES:
                    found.append((child.lineno, function))
            visit(child, scope, function)

    visit(tree, _Scope(tree), "<module>")
    return found


def _policed(path: Path) -> bool:
    parts = path.relative_to(src_root()).parts
    return not (
        "_vendor" in parts
        or "tests" in parts
        or path.name.startswith("test_")
        or path.name == "conftest.py"
    )


def _tree_violations() -> list[str]:
    out: list[str] = []
    # Raw-text matching is sound here: the needles are string-literal CONTENT,
    # which the parser never normalises (only identifiers are NFKC-folded).
    for path, text in iter_source_texts():
        if not any(s in text for s in _LEAF_SPELLINGS) or not _policed(path):
            continue
        rel = path.relative_to(src_root()).as_posix()
        out.extend(f"{rel}:{line} in {fn}()" for line, fn in find_violations(text))
    return out


def test_no_strict_text_read_of_a_proc_comm_carrier_in_the_package() -> None:
    violations = _tree_violations()
    assert not violations, (
        "a strict text read of /proc/<pid>/stat|status|comm raises UnicodeDecodeError "
        "on a process whose name is not UTF-8. Use platform_compat.read_proc_stat for "
        "stat, a bytes read decoded with surrogateescape for comm, or errors='replace' "
        "for status:\n  " + "\n  ".join(violations)
    )


def _flagged(source: str) -> bool:
    return bool(find_violations(source))


class TestWhatTheGateCatches:
    @pytest.mark.parametrize("leaf", sorted(PROC_LEAVES))
    def test_an_fstring_read_text_of_each_leaf(self, leaf: str) -> None:
        assert _flagged(f'from pathlib import Path\nPath(f"/proc/{{pid}}/{leaf}").read_text()\n')

    def test_a_join_onto_a_directory_entry(self) -> None:
        assert _flagged('for entry in roots:\n    (entry / "stat").read_text()\n')

    def test_an_encoding_without_an_error_handler_is_still_strict(self) -> None:
        assert _flagged('Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")\n')

    def test_errors_strict_is_no_handler(self) -> None:
        assert _flagged('Path(f"/proc/{pid}/comm").read_text(errors="strict")\n')

    def test_a_text_mode_open(self) -> None:
        assert _flagged('with open(f"/proc/{pid}/status", encoding="ascii") as fh:\n    pass\n')

    def test_a_path_open_in_text_mode(self) -> None:
        assert _flagged('(root / str(pid) / "status").open()\n')

    def test_a_strict_decode_of_a_bytes_read(self) -> None:
        assert _flagged('(root / "comm").read_bytes().decode("utf-8")\n')

    @pytest.mark.parametrize(
        "call",
        [
            'open(f"/proc/{pid}/status", "r", -1, "utf-8")',
            'open(f"/proc/{pid}/status", "r", -1, "utf-8", "strict")',
            '(root / str(pid) / "status").open("r", -1, "utf-8")',
            '(root / str(pid) / "status").open("r", -1, "utf-8", "strict")',
        ],
    )
    def test_a_positional_encoding_is_not_an_error_handler(self, call: str) -> None:
        assert _flagged(call + "\n")

    def test_a_path_held_in_a_module_constant(self) -> None:
        source = (
            '_STATUS = Path("/proc/self/status")\n'
            "def peak():\n"
            '    return _STATUS.read_text(encoding="utf-8")\n'
        )
        assert _flagged(source)

    def test_a_path_bound_in_an_enclosing_function(self) -> None:
        source = (
            "def outer(pid):\n"
            '    p = f"/proc/{pid}/comm"\n'
            "    def inner():\n"
            "        return open(p).read()\n"
            "    return inner\n"
        )
        assert _flagged(source)

    def test_a_path_bound_to_a_name_first(self) -> None:
        source = (
            "def f(pid, proc_root):\n"
            '    p = Path(f"/proc/{pid}/stat") if proc_root is None else proc_root / "stat"\n'
            "    return p.read_text()\n"
        )
        assert _flagged(source)


class TestWhatTheGateLeavesAlone:
    def test_a_bytes_read(self) -> None:
        assert not _flagged('(entry / "stat").read_bytes()\n')

    def test_a_binary_open(self) -> None:
        assert not _flagged('with open(f"/proc/{pid}/stat", "rb") as fh:\n    pass\n')

    def test_a_replace_or_surrogateescape_decode(self) -> None:
        assert not _flagged('Path(f"/proc/{pid}/stat").read_text(errors="replace")\n')
        assert not _flagged('(r / "comm").read_bytes().decode("utf-8", "surrogateescape")\n')
        assert not _flagged('open(f"/proc/{p}/status", encoding="utf-8", errors="replace")\n')

    def test_a_positional_error_handler(self) -> None:
        assert not _flagged('open(f"/proc/{p}/status", "r", -1, "utf-8", "replace")\n')
        assert not _flagged('(r / str(p) / "status").open("r", -1, "utf-8", "replace")\n')

    def test_a_local_name_shadows_a_module_constant(self) -> None:
        source = (
            '_P = Path("/proc/self/status")\n'
            "def f():\n"
            '    _P = Path("/proc/self/statm")\n'
            "    return _P.read_text()\n"
        )
        assert not _flagged(source)

    def test_another_proc_leaf(self) -> None:
        """``statm``, ``uptime`` and ``cmdline`` carry no comm (cmdline is read as bytes)."""
        assert not _flagged('(root / str(pid) / "statm").read_text()\n')
        assert not _flagged('Path("/proc/uptime").read_text()\n')

    def test_the_host_wide_proc_stat(self) -> None:
        assert not _flagged('with open("/proc/stat") as f:\n    pass\n')

    def test_a_file_that_merely_contains_the_word(self) -> None:
        assert not _flagged('Path("memory.stat").read_text()\n')

    def test_os_open_returns_a_descriptor_not_text(self) -> None:
        assert not _flagged('os.open(f"/proc/{pid}/stat", os.O_RDONLY)\n')
