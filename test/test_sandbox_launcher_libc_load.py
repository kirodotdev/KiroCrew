"""The namespace launcher must resolve libc via ``dlopen(NULL)``, never PATH.

The launcher's module scope runs BEFORE its ``fork()`` and before either
``unshare()``, under an environment the SPAWNING CALLER supplies -- and a
caller-declared ``PATH`` does reach it (``cron_script`` forwards a per-server
``env`` block; ``mcp_discovery`` composes a declared ``PATH`` into the probe env).

``ctypes.util.find_library("c")`` is therefore unsafe there. On Linux it
EXECUTES helper processes to locate libc: ``_findSoname_ldconfig`` first
(absolute ``/sbin/ldconfig``, env carrying no ``PATH``, so PATH-immune), and
only when that yields no match -- musl/Alpine, or no ``ldconfig`` -- the
PATH-resolving ``_findLib_gcc`` (``shutil.which('gcc')`` / ``'cc'``),
``_get_soname`` (``objdump``) and ``_findLib_ld`` (bare ``ld``). On such a host a
caller-controlled ``gcc`` on ``PATH`` would be same-user code execution ahead of
the confinement the launcher exists to establish.

The spawned userns probe already followed this rule; these tests pin the
launcher to it too, so the two pre-confinement scripts cannot drift apart again.

Two deliberate shapes here:

* Assertions run over the parsed AST, not the source text. Every one of these
  scripts *documents* the rule in a comment naming ``find_library``, so a
  substring match would be satisfied by the prose and would pass on a script
  that still calls it.
* The scripts are built inside a fixture rather than at module scope, and the
  module is Linux-marked. ``_build_launcher_script`` calls ``os.getuid()``,
  which does not exist on Windows -- building at import time would crash
  COLLECTION there rather than skipping, and macOS libc carries no ``unshare``
  for the symbol check below. The launcher is Linux-only in production, so
  there is nothing to assert elsewhere.
"""

from __future__ import annotations

import ast
import ctypes
import sys

import pytest

from kiro_crew.sandbox import _PROBE_SHIM_CODE, _build_launcher_script

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the namespace launcher is Linux-only: _build_launcher_script uses "
    "os.getuid() (absent on Windows) and binds unshare() (absent on macOS libc)",
)

#: Labels for every pre-confinement script this module generates. Both kinds run
#: with a caller-supplied environment before any namespace exists, so both are
#: bound by the no-``find_library`` rule -- asserting over the pair is what stops
#: a future change from fixing one and reopening the other.
_PRECONFINEMENT_LABELS = ("launcher(standard)", "launcher(strict)", "probe shim")


def _source_for(label: str) -> str:
    if label == "probe shim":
        return _PROBE_SHIM_CODE
    mode = label[len("launcher(") : -1]
    return _build_launcher_script(mode)


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
    """A pre-confinement script's label and parsed tree.

    Parsing here doubles as a syntax regression on the generated launcher: an
    f-string template that produced invalid Python would fail every test in this
    module rather than only surfacing at spawn time.
    """
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


def test_dlopen_null_exposes_the_syscalls_the_launcher_binds() -> None:
    """``dlopen(NULL)`` really carries the symbols, not just the right spelling.

    Guards the substitution rather than its source text: had ``CDLL(None)`` not
    carried these, every assertion above would still pass while each namespace
    spawn died at the launcher's module scope. ``prctl`` is excluded -- the
    launcher already treats it as optional via ``hasattr``.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    for symbol in ("mount", "unshare"):
        assert hasattr(libc, symbol), (
            f"dlopen(NULL) does not expose {symbol}(); the launcher binds it at "
            "module scope and would fail to start"
        )
