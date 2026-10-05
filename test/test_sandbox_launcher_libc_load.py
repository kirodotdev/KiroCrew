"""The namespace launcher must resolve libc via ``dlopen(NULL)``, never PATH.

The launcher loads libc BEFORE its ``fork()`` and before either ``unshare()``, under
an environment the SPAWNING CALLER supplies -- and a caller-declared ``PATH`` does
reach it (``cron_script`` forwards a per-server ``env`` block; ``mcp_discovery``
composes a declared ``PATH`` into the probe env).

``ctypes.util.find_library("c")`` is therefore unsafe there. On Linux it
EXECUTES helper processes to locate libc: ``_findSoname_ldconfig`` first
(absolute ``/sbin/ldconfig``, env carrying no ``PATH``, so PATH-immune), and
only when that yields no match -- musl/Alpine, or no ``ldconfig`` -- the
PATH-resolving ``_findLib_gcc`` (``shutil.which('gcc')`` / ``'cc'``),
``_get_soname`` (``objdump``) and ``_findLib_ld`` (bare ``ld``). On such a host a
caller-controlled ``gcc`` on ``PATH`` would be same-user code execution ahead of
the confinement the launcher exists to establish.

The spawned userns probe already followed this rule; these tests hold the
launcher to it too, so the two pre-confinement scripts cannot drift apart again.

The launcher is ``kiro_crew.sandbox_launcher_program``, and the file a spawn runs is
that module's source with one plan line substituted
(``test_sandbox_launcher_program.py`` holds that). So:

* Its libc load is CALLED: ``main`` -- the entry point the rendered file runs --
  loads libc through ``_load_libc``, here with ``ctypes.CDLL`` and
  ``ctypes.util.find_library`` replaced by recorders, so the call it makes is
  observed rather than read. A rendered launcher is also loaded in a fresh
  isolated interpreter, which is the only place "``ctypes.util`` is never
  imported" can be observed: this test process imports it for other reasons.
* The whole-program rules -- no call ANYWHERE resolves libc through a lookup, every
  ``CDLL`` is ``CDLL(None)`` -- are read from the parsed AST of the program's
  source (``sandbox_launcher.launcher_program_source``, the text the renderer
  substitutes into), because no finite set of runs covers every branch. The probe
  shim is a source string handed to ``python -c``, so it is read the same way.
  AST, not substring: both scripts *document* the rule in a comment naming
  ``find_library``, so a substring match would be satisfied by the prose.

Linux-marked: the launcher is Linux-only in production, and macOS libc carries no
``unshare`` for the symbol check below.
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.util
import subprocess
import sys
import types
from pathlib import Path

import pytest
from test_sandbox_launcher_program import payload, refusal

from kiro_crew import sandbox_launcher, sandbox_launcher_program, sandbox_plan
from kiro_crew.sandbox import _PROBE_SHIM_CODE

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the namespace launcher is Linux-only: it binds unshare() (absent on macOS "
    "libc) and its payload carries os.getuid() (absent on Windows)",
)


#: Labels for every pre-confinement script. Both run with a caller-supplied
#: environment before any namespace exists, so both are bound by the
#: no-``find_library`` rule -- asserting over the pair is what stops a future change
#: from fixing one and reopening the other.
_PRECONFINEMENT_LABELS = ("launcher program", "probe shim")


def _source_for(label: str) -> str:
    if label == "probe shim":
        return _PROBE_SHIM_CODE
    return sandbox_launcher.launcher_program_source()


def _dotted_name(node: ast.AST) -> str:
    """Render an ``ast`` call target as its dotted source spelling."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _called_names(tree: ast.AST) -> list[str]:
    return [_dotted_name(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]


@pytest.fixture(params=_PRECONFINEMENT_LABELS, ids=_PRECONFINEMENT_LABELS)
def preconfinement(request: pytest.FixtureRequest) -> tuple[str, ast.Module]:
    """A pre-confinement script's label and parsed tree."""
    label = request.param
    return label, ast.parse(_source_for(label))


def test_never_resolves_libc_through_path(preconfinement: tuple[str, ast.Module]) -> None:
    """No pre-confinement script may CALL a PATH-resolving libc lookup."""
    label, tree = preconfinement
    offenders = [n for n in _called_names(tree) if n.endswith("find_library")]
    assert not offenders, (
        f"{label} calls {offenders} to resolve libc, which execs a PATH-resolved "
        "gcc/cc/objdump/ld on musl hosts -- before this script establishes "
        "confinement, under a caller-supplied PATH"
    )


def test_loads_libc_via_dlopen_null(preconfinement: tuple[str, ast.Module]) -> None:
    """libc comes from ``ctypes.CDLL(None)`` -- the already-mapped libc."""
    label, tree = preconfinement
    loads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _dotted_name(n.func).endswith("CDLL") and n.args
    ]
    assert loads, f"{label} never loads libc through ctypes.CDLL"
    for call in loads:
        first = call.args[0]
        assert isinstance(first, ast.Constant) and first.value is None, (
            f"{label} passes a computed path to CDLL; it must be the literal "
            "None (dlopen(NULL)) so no lookup runs pre-confinement"
        )


def test_does_not_import_ctypes_util(preconfinement: tuple[str, ast.Module]) -> None:
    """``ctypes.util`` stays unimported so a reintroduction fails loudly.

    Without the import, a future ``find_library`` call in these scripts raises
    ``AttributeError`` at spawn instead of silently reopening the PATH lookup --
    the import's absence is the guard, not incidental tidiness.
    """
    label, tree = preconfinement
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module}.{a.name}" for a in node.names]
    assert "ctypes.util" not in imported, f"{label} imports ctypes.util"


class _RecordedLibc:
    """What ``ctypes.CDLL`` returns here: every function the launcher binds, as a slot."""

    def __init__(self) -> None:
        for name in ("mount", "unshare", "umount2", "prctl"):
            setattr(self, name, types.SimpleNamespace(argtypes=None, restype=None))


def test_the_launcher_entry_point_loads_libc_by_dlopen_null_and_never_looks_it_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` with no libc handed in takes the process's own, through ``CDLL(None)``.

    Called with no agent command, ``main`` loads libc and then refuses before it
    forks, so the load it makes is the whole of what runs.
    """
    loads: list[tuple[tuple[object, ...], dict[str, object]]] = []
    lookups: list[str] = []

    def _cdll(*args: object, **kwargs: object) -> _RecordedLibc:
        loads.append((args, kwargs))
        return _RecordedLibc()

    def _find_library(name: str) -> None:
        lookups.append(name)

    monkeypatch.setattr(program.ctypes, "CDLL", _cdll)
    monkeypatch.setattr(ctypes.util, "find_library", _find_library)

    message = refusal(program.main, payload(), None, [])

    assert message == "sandbox_launcher: no command given"
    assert loads == [
        ((None,), {"use_errno": True})
    ], "the launcher must load the already-mapped libc with dlopen(NULL), once"
    assert lookups == [], "the launcher looked libc up, which runs a PATH-resolved helper"


def test_a_rendered_launcher_never_imports_ctypes_util(tmp_path: Path) -> None:
    """Loaded and asked for its libc in a fresh interpreter, ``ctypes.util`` stays absent.

    ``-I -S`` is how a spawn runs the launcher, so nothing but the program's own
    imports can put the module there. Loaded under a module name other than
    ``__main__``, so the program defines its stages and does not start.
    """
    plan = sandbox_plan.plan_confinement(
        sandbox_plan.SandboxRequest(tier="strict"), sandbox_plan.PlanHost(home=str(tmp_path))
    )
    script = tmp_path / "kirocrew_sandbox_libc_load.py"
    script.write_text(sandbox_launcher.render_namespace_launcher(plan), encoding="utf-8")
    driver = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('launcher_under_test', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module._load_libc()\n"
        "print('ctypes.util' in sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", driver, str(script)],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
        cwd=str(tmp_path),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False", "the launcher program imported ctypes.util"


def test_dlopen_null_exposes_the_syscalls_the_launcher_binds() -> None:
    """``dlopen(NULL)`` really carries the symbols, not just the right spelling.

    Runs the launcher's own libc load for real: had ``CDLL(None)`` not carried these,
    every assertion above would still pass while each namespace spawn died binding
    them before it forked. ``prctl`` is excluded -- the launcher treats it as optional
    and every stage checks for it.
    """
    libc = program._load_libc()
    for symbol in ("mount", "unshare", "umount2"):
        assert getattr(libc, symbol).restype is ctypes.c_int, (
            f"dlopen(NULL) does not expose {symbol}(); the launcher binds it before it "
            "forks and would fail to start"
        )
