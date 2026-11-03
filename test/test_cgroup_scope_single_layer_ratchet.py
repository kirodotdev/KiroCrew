"""A spawned argv carries exactly ONE ``systemd-run --scope`` layer.

``sandboxed_spawn_argv`` applies ``cgroup_scope_argv`` as its outermost layer, so
its result is already inside a transient scope. Passing that result through
``cgroup_scope_argv`` again nests a second ``systemd-run --user --scope``. Because
``--scope`` execs in place, the inner call keeps the outer one's PID and asks for
the same auto-named ``run-p<PID>-i<N>.scope`` unit, which newer systemd refuses,
so the command never runs. A spawn test that mocks the subprocess cannot see the
nesting, so only an argv-level check counts the layers.

Two layers of pinning:

* the real chokepoint, with the cgroup backend forced available, applies its scope
  as the outermost and only layer (an app pins its own spawn sites against the same
  chokepoint in its own ``tests/``, as PPTX Maker does);
* a source sweep over ``src/kiro_crew`` refuses any function that hands a
  ``sandboxed_spawn_argv`` / ``sandboxed_spawn_argv_async`` result to
  ``cgroup_scope_argv``, so the next caller cannot bring the double wrap back.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew.sandbox as sb

_SYSTEMD_RUN = "/usr/bin/systemd-run"
_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
_CHOKEPOINTS = frozenset({"sandboxed_spawn_argv", "sandboxed_spawn_argv_async"})


@pytest.fixture
def cgroup_backend_available(monkeypatch):
    """Force ``cgroup_scope_argv`` to wrap, independent of the test host.

    Same stubs as ``test_per_runtime_process_resources._wrap``: the probe, the
    trusted ``systemd-run`` resolution, the slice reconcile and the limits are
    all replaced, so this runs identically on Linux, macOS and Windows.
    """
    monkeypatch.setattr(sb, "_CGROUP_SCOPE_PROBE", None)
    monkeypatch.setattr(sb, "_CGROUP_WARNED", False)
    monkeypatch.setattr(sb, "_probe_cgroup_scope", lambda: (True, "ok"))
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda name: _SYSTEMD_RUN)
    monkeypatch.setattr(sb, "_reconcile_slice_memory_high_off_thread", lambda *a, **k: None)
    monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda _rl=None: (8192, 8192, 50, 0))
    monkeypatch.setattr(sb, "_cpu_controller_delegated", lambda: False)


@pytest.fixture
def real_chokepoint_without_os_sandbox(monkeypatch):
    """Run the REAL ``sandboxed_spawn_argv``, minus only its OS-sandbox layer.

    The namespace/seatbelt layer underneath is host-dependent and adds no scope,
    so ``wrap_argv`` passes the argv through. Everything after it, including the
    chokepoint's own outermost ``cgroup_scope_argv``, runs as shipped, so these
    tests go red on a second wrap at the call site AND on the chokepoint
    dropping its own (which would leave the spawn with no ceiling at all).
    """
    monkeypatch.setattr(sb, "wrap_argv", lambda argv, **_kw: (list(argv), None))


def _scope_layers(argv: list[str]) -> int:
    return sum(1 for a in argv if a == _SYSTEMD_RUN)


class TestTheChokepointCarriesOneScope:
    def test_the_fixture_really_wraps(self, cgroup_backend_available):
        """Guard the guard: with the backend forced, one wrap is one layer."""
        assert _scope_layers(sb.cgroup_scope_argv(["/bin/true"])) == 1

    def test_the_chokepoint_applies_the_scope_outermost(
        self, cgroup_backend_available, real_chokepoint_without_os_sandbox
    ):
        wrapped, _env, _cleanup = sb.sandboxed_spawn_argv(
            ["/bin/true"], mode="strict", strip_python_env=True
        )
        assert wrapped[0] == _SYSTEMD_RUN
        assert _scope_layers(wrapped) == 1, wrapped


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Subscript):
        node = node.value
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _argv_target(target: ast.AST) -> str | None:
    """The name bound to the ARGV of an assignment (first element of a tuple)."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name):
        return target.elts[0].id
    return None


def _double_wraps(source: str) -> list[int]:
    """Line numbers where a chokepoint result reaches ``cgroup_scope_argv``.

    Per function: each name is tainted by its LATEST assignment before the call,
    so ``cmd = sandboxed_spawn_argv(...)[0]`` followed by ``cmd = wrap_argv(x)``
    is clean, and a direct ``cgroup_scope_argv(sandboxed_spawn_argv(...)[0])``
    is caught.
    """
    hits: list[int] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bindings: list[tuple[int, str, bool]] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                tainted = _call_name(node.value) in _CHOKEPOINTS
                for target in node.targets:
                    name = _argv_target(target)
                    if name:
                        bindings.append((node.lineno, name, tainted))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _call_name(node) != "cgroup_scope_argv":
                continue
            arg = node.args[0]
            if _call_name(arg) in _CHOKEPOINTS:
                hits.append(node.lineno)
                continue
            if isinstance(arg, ast.Name):
                prior = [b for b in bindings if b[1] == arg.id and b[0] < node.lineno]
                if prior and max(prior)[2]:
                    hits.append(node.lineno)
    return sorted(hits)


class TestNoCallerReWrapsTheChokepoint:
    def test_the_detector_catches_both_shapes_and_spares_a_rebinding(self):
        """Guard the guard, so an empty sweep means clean, not blind."""
        source = (
            "def a(argv):\n"
            "    w, env, c = sandboxed_spawn_argv(argv)\n"
            "    w = cgroup_scope_argv(w)\n"
            "async def b(argv):\n"
            "    w, env, c = await sandboxed_spawn_argv_async(argv)\n"
            "    return cgroup_scope_argv(w)\n"
            "def c(argv):\n"
            "    return cgroup_scope_argv(sb.sandboxed_spawn_argv(argv)[0])\n"
            "async def d(argv):\n"
            "    cmd, env, c = await sandboxed_spawn_argv_async(argv)\n"
            "    cmd, c = await wrap_argv_async(argv)\n"
            "    return cgroup_scope_argv(cmd)\n"
        )
        assert _double_wraps(source) == [3, 6, 8]

    def test_no_function_in_src_scopes_a_chokepoint_result_twice(self):
        offenders = []
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            # The detector matches the call by name, so a file that never spells
            # it cannot hold a hit; skipping it keeps the sweep off the full parse.
            if "cgroup_scope_argv" not in source:
                continue
            for line in _double_wraps(source):
                offenders.append(f"{path.relative_to(_SRC_ROOT).as_posix()}:{line}")
        assert not offenders, (
            "sandboxed_spawn_argv already applies cgroup_scope_argv as its outermost "
            "layer; wrapping its result again nests a second systemd-run --scope that "
            "collides on the outer scope's unit name. Drop the second wrap at:\n  "
            + "\n  ".join(offenders)
        )
