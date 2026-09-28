"""Structural pin: in the reaper's kill path, a process is a ``ProcessHandle``, never a bare pid.

A pid is a number the kernel reuses. Every defect in the identity family that
this pin closes had the same shape: something in the kill path used the pid
ALONE as the process's identity -- a dedup set keyed by pid that filed a recycled
pid's successor under its dead predecessor, a group signal that resolved the group
from the pid at signal time (``os.getpgid(pid)``) and so named whatever process the
kernel had handed the number to. The rule the code follows now: the kill consumes
``ProcessHandle`` (pid + start id, hashed and compared as one), the POSIX group it
signals is the one CAPTURED with the handle (``platform_compat.kill_process_group``
takes that id and resolves nothing), and the only pid-addressed calls left are the
capture site itself and the two pid-scoped kills of a pid whose identity was read
back an instant before.

This module reads the reaper modules' source with :mod:`ast` and fails on:

* a call to a pid-addressed primitive -- ``os.getpgid`` / ``os.killpg`` /
  ``os.kill`` (also as the ``getattr``-resolved bare names ``getpgid`` /
  ``killpg``) or ``platform_compat.kill_process_tree`` / ``kill_process_tree_async``
  / ``kill_pid`` / ``kill_pid_async`` -- anywhere except the allowlisted verified
  helpers in ``process_identity.py`` (:data:`ALLOWED_PID_CALLS`);
* a comparison that uses ``.pid`` as identity -- ``a.pid == b.pid``, ``x.pid in
  ...`` -- anywhere in the four modules;
* an annotated ``set[int]`` / ``dict[int, ...]`` whose name says ``pid`` and is not
  one of the named orphan-sweep shields (:data:`ALLOWED_PID_COLLECTIONS`), which
  compare against live pids the sweep itself enumerates and are not kill addresses.

The allowlists are exact names: adding a pid-addressed call means adding it here
WITH its justification, in the same change.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: The modules that hold the reaper's kill path: the cron reaper and ``cancel()``,
#: the identity/kill module, and the session facade and allocation boundary the
#: reaper drives (the torn-down table, the ending fence, the handle snapshot).
REAPER_MODULES = (
    "cron.py",
    "process_identity.py",
    "session.py",
    "session_allocation.py",
)

#: Attribute names that address a process by its bare pid when called.
PID_ADDRESSED_ATTRS = frozenset(
    {
        "getpgid",
        "killpg",
        "kill",
        "kill_process_tree",
        "kill_process_tree_async",
        "kill_pid",
        "kill_pid_async",
    }
)

#: The ``getattr(os, ...)``-resolved locals ``process_identity`` calls by bare name.
PID_ADDRESSED_BARE_NAMES = frozenset({"getpgid", "killpg"})

#: module -> function -> the pid-addressed callee names that function may call, and why.
ALLOWED_PID_CALLS: dict[str, dict[str, frozenset[str]]] = {
    "process_identity.py": {
        # The capture site: ``getpgid(pid)`` bracketed by two identity reads,
        # kept only for an isolated leader -- the one place a group id comes from.
        "isolated_group_of": frozenset({"getpgid"}),
        # The pid-scoped kill of a leader whose start id was read back two
        # syscalls earlier and for which no group was captured: the verified pid
        # IS the address, nothing is resolved from it.
        "kill_verified_process": frozenset({"kill_pid_async"}),
        # EPERM on the captured group with the leader verified alive: the same
        # pid-scoped kill of the just-verified leader, never for a gone one.
        "_signal_retained_group": frozenset({"kill_pid_async"}),
    },
}

#: Named ``set[int]`` pid collections outside the kill path: the session
#: manager's orphan-sweep and warm-pool shields, which the periodic sweep
#: compares against the live pids IT enumerates from the process table (a shield
#: is "do not kill this number right now", never a kill address). Converting them
#: to handles is the sweep's change, not the reaper's.
ALLOWED_PID_COLLECTIONS: dict[str, frozenset[str]] = {
    "session.py": frozenset(
        {
            "_starting_pids",
            "_pool_sweep_pids",
            "_pool_pids",
            "_in_flight_pids",
            "_companion_runtime_pids",
            "pids",
        }
    ),
    "session_allocation.py": frozenset({"starting_pids", "_starting_pids"}),
}


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_pid_attribute(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "pid"


def _is_int_keyed_collection(annotation: ast.AST) -> bool:
    """``set[int]`` / ``frozenset[int]`` / ``dict[int, ...]`` annotations."""
    if not isinstance(annotation, ast.Subscript):
        return False
    base = annotation.value
    base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
    if base_name not in {"set", "frozenset", "dict", "Set", "FrozenSet", "Dict"}:
        return False
    first = annotation.slice
    if isinstance(first, ast.Tuple):
        first = first.elts[0] if first.elts else first
    return isinstance(first, ast.Name) and first.id == "int"


class _Scan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.findings: list[str] = []

    def _where(self, node: ast.AST) -> str:
        scope = ".".join(self.stack) or "<module>"
        return f"{self.module}:{node.lineno} in {scope}"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node)
        pid_addressed = False
        if isinstance(node.func, ast.Attribute) and name in PID_ADDRESSED_ATTRS:
            owner = node.func.value
            owner_name = (
                owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            )
            # ``os.kill`` / ``platform_compat.kill_pid`` and friends; ``task.cancel``-style
            # attributes named ``kill`` on other objects are not process signals.
            pid_addressed = owner_name in {"os", "platform_compat"} or name not in {"kill"}
        elif isinstance(node.func, ast.Name) and name in PID_ADDRESSED_BARE_NAMES:
            pid_addressed = True
        if pid_addressed and name is not None:
            allowed = ALLOWED_PID_CALLS.get(self.module, {})
            enclosing = self.stack[-1] if self.stack else "<module>"
            if name not in allowed.get(enclosing, frozenset()):
                self.findings.append(
                    f"{self._where(node)}: pid-addressed call `{ast.unparse(node.func)}(...)` "
                    "outside the allowlisted verified helpers"
                )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        pid_operands = [op for op in operands if _is_pid_attribute(op)]
        if len(pid_operands) >= 2 and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            self.findings.append(
                f"{self._where(node)}: `{ast.unparse(node)}` compares processes by pid; "
                "compare the handles"
            )
        elif pid_operands and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            self.findings.append(
                f"{self._where(node)}: `{ast.unparse(node)}` keys a collection by pid; "
                "key it by the handle"
            )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        target = node.target
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
        self._check_collection(node, name, node.annotation)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if node.annotation is not None:
            self._check_collection(node, node.arg, node.annotation)
        self.generic_visit(node)

    def _check_collection(self, node: ast.AST, name: str, annotation: ast.AST) -> None:
        if "pid" not in name.lower() or not _is_int_keyed_collection(annotation):
            return
        if name in ALLOWED_PID_COLLECTIONS.get(self.module, frozenset()):
            return
        self.findings.append(
            f"{self._where(node)}: `{name}: {ast.unparse(annotation)}` is a pid-keyed "
            "collection; key it by ProcessHandle"
        )


def _scan(module: str) -> list[str]:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"), filename=module)
    scan = _Scan(module)
    scan.visit(tree)
    return scan.findings


@pytest.mark.parametrize("module", REAPER_MODULES)
def test_the_kill_path_never_uses_a_bare_pid_as_identity(module: str) -> None:
    findings = _scan(module)
    assert not findings, (
        f"{len(findings)} bare-pid identity use(s) in {module} -- a pid is a number the "
        "kernel reuses; the kill path keys by ProcessHandle and signals the captured group:\n  "
        + "\n  ".join(findings)
    )


def test_the_allowlist_names_only_functions_that_exist() -> None:
    """An allowlist entry for a function that was renamed away would silently stop guarding it."""
    for module, functions in ALLOWED_PID_CALLS.items():
        tree = ast.parse((SRC / module).read_text(encoding="utf-8"), filename=module)
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = set(functions) - defined
        assert not missing, f"{module}: allowlisted functions not defined: {sorted(missing)}"


def test_the_pin_sees_a_bare_pid_use() -> None:
    """Self-test: the scan flags each shape the pin exists for."""
    source = (
        "import os\n"
        "def a(handles, candidate):\n"
        "    return any(h.pid == candidate.pid for h in handles)\n"
        "def b(pid):\n"
        "    return os.killpg(os.getpgid(pid), 9)\n"
        "def c(handle, seen: set[int]):\n"
        "    return handle.pid in seen\n"
    )
    scan = _Scan("cron.py")
    scan.visit(ast.parse(source))
    text = "\n".join(scan.findings)
    assert "compares processes by pid" in text
    assert "`os.killpg(...)`" in text and "`os.getpgid(...)`" in text
    assert "keys a collection by pid" in text
    assert "`seen: set[int]` is a pid-keyed collection" not in text  # the name says nothing of pids
    assert scan.findings and len(scan.findings) == 4
