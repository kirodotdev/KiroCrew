"""The namespace launcher must import everything BEFORE entering isolation.

A first-time stdlib import reads module files off disk. The launcher's Steps
5/6 (capability drop, seccomp install) run in the child AFTER
``unshare(NEWUSER)`` + ``unshare(NEWNS)`` + the mount masking, and on a host
whose LSM restricts unprivileged user namespaces that post-unshare read is
denied: Ubuntu 24.04 with ``apparmor_restrict_unprivileged_userns=1`` killed
``import platform`` at seccomp-install time with ``ModuleNotFoundError``, so
every sandboxed spawn died inside the launcher (#8151). The isolation probe in
Auto-Improvement then read that crash as "push is not disabled".

The structural rule these tests pin: every ``import`` in the generated launcher
is MODULE-LEVEL. Module scope runs pre-fork, pre-unshare, so a module-level
import can never hit the post-isolation filesystem denial; an import nested in
any function/branch executes wherever its enclosing code does, which for this
script means potentially after isolation. Asserting "module-level only" is
stricter than "not after unshare" but needs no control-flow analysis and leaves
no room for a future lazy import to creep back in past the exact line the bug
sat on.

Assertions run over the parsed AST, not the source text: the launcher's
comments narrate the hoist (naming ``import platform``), so a substring match
would pass on prose alone. Built inside a fixture and Linux-marked for the same
reasons as ``test_sandbox_launcher_libc_load.py``: ``_build_launcher_script``
calls ``os.getuid()``, absent on Windows.
"""

from __future__ import annotations

import ast
import sys

import pytest

from kiro_crew.sandbox import _build_launcher_script

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the namespace launcher is Linux-only: _build_launcher_script uses "
    "os.getuid() (absent on Windows)",
)

#: Every launcher variant: the template branches on level (dir lists, expose
#: files), so asserting one level could miss an import reintroduced in a branch
#: another level renders.
_LEVELS = ("standard", "cc", "strict")


@pytest.fixture(params=_LEVELS, ids=_LEVELS)
def launcher_tree(request: pytest.FixtureRequest) -> ast.Module:
    """Parsed launcher for one sandbox level.

    Parsing doubles as a syntax regression on the generated f-string template.
    """
    return ast.parse(_build_launcher_script(request.param))


def test_every_launcher_import_is_module_level(launcher_tree: ast.Module) -> None:
    """No import may execute after namespace/mount isolation.

    The only sanctioned position is a direct child of the module body, which
    runs before ``fork()`` and therefore before either ``unshare()``. Imports
    nested in module-level ``if``/``try`` blocks are refused too: they would
    pass a naive indentation check while still being conditional.
    """
    module_level = {id(node) for node in launcher_tree.body}
    offenders = [
        f"line {node.lineno}: {ast.dump(node)[:120]}"
        for node in ast.walk(launcher_tree)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in module_level
    ]
    assert not offenders, (
        "the generated launcher contains non-module-level import(s); a first-time "
        "import after unshare()+mount isolation is denied on LSM-restricted hosts "
        "(#8151) — hoist them into the preamble:\n" + "\n".join(offenders)
    )


def test_post_isolation_modules_are_imported_in_preamble(launcher_tree: ast.Module) -> None:
    """The two modules Steps 5/6 use must be present at module level.

    Guards the other direction: deleting the hoisted imports would satisfy the
    module-level-only test while breaking the launcher at runtime.
    """
    imported = {
        alias.name
        for node in launcher_tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert {"platform", "struct"} <= imported, (
        f"launcher preamble imports {sorted(imported)} — 'platform' (seccomp arch "
        "table) and 'struct' (BPF packing) are used after isolation and must be "
        "imported before it"
    )


def test_syspath_purge_precedes_every_filesystem_import(launcher_tree: ast.Module) -> None:
    """The stdlib-shadowing hardening must stay ahead of the hoisted imports.

    The launcher strips its own script directory from ``sys.path`` before any
    import that resolves from the filesystem — a sibling ``struct.py`` left in
    the run/ directory would otherwise shadow the real stdlib. Module-level
    placement alone (the tests above) stays green if a future edit moves an
    import ABOVE that purge, silently reinstating the shadowing bug. Only
    ``import sys`` may precede it: ``sys`` is a builtin and cannot be shadowed,
    and the purge itself needs it. Raised by the Opus review of this branch.
    """
    purge_index = next(
        (
            i
            for i, node in enumerate(launcher_tree.body)
            if isinstance(node, ast.Assign) and ast.unparse(node).startswith("sys.path[:] =")
        ),
        None,
    )
    assert purge_index is not None, "the sys.path shadowing purge is gone from the launcher"
    for i, node in enumerate(launcher_tree.body):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        if names == ["sys"]:
            continue
        assert i > purge_index, (
            f"launcher line {node.lineno} imports {names or ast.dump(node)[:60]} BEFORE the "
            "sys.path purge — a sibling module in the launcher's own directory would "
            "shadow the real stdlib there"
        )


@pytest.mark.parametrize("level", _LEVELS)
def test_probe_failure_markers_round_trip_against_the_launcher(level: str) -> None:
    """The probe's launcher-failure signature must track what the launcher emits.

    ``clone_setup._LAUNCHER_EXIT_PREFIXES`` classifies a nonzero probe exit as
    a launcher failure; each prefix must exist in the generated launcher (and
    the traceback marker must match the launcher's real filename prefix), or
    the list has drifted and real launcher deaths fall back to the misleading
    push-isolation refusal this pairing exists to prevent (#8151).
    """
    import inspect

    import kiro_crew.sandbox as sandbox_mod
    from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
        _LAUNCHER_EXIT_PREFIXES,
    )

    script = _build_launcher_script(level)
    for prefix in _LAUNCHER_EXIT_PREFIXES:
        assert prefix in script, (
            f"probe marker {prefix!r} no longer appears in the generated launcher — "
            "update clone_setup._LAUNCHER_EXIT_PREFIXES together with the launcher"
        )
    # The traceback-frame marker keys on the launcher's on-disk filename prefix.
    assert 'prefix=f"kirocrew_sandbox_' in inspect.getsource(sandbox_mod), (
        "the launcher's tempfile prefix changed — update the traceback regex in "
        "clone_setup (_LAUNCHER_TRACEBACK_RE) to match"
    )
