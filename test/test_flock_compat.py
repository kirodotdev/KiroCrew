"""Tests for the cross-platform flock shim (enables the Windows cloud client)."""

from __future__ import annotations

import ast
import builtins
import importlib
import sys


def _unconditional_fcntl_imports(source: str) -> list[int]:
    """Line numbers of top-level, unguarded imports of ``fcntl`` in *source*.

    Walks the parsed module body only: an ``import`` nested in ``if``/``try``
    (the tree's platform guards), a function or a class executes conditionally
    or later, and string literals (a rendered launcher template, a docstring)
    never execute at all -- none of those can crash a Windows import of the
    module. Kept dependency-free and self-contained: the CLI-graph test ships
    this exact source into a fresh interpreter.
    """
    hits = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "fcntl" for alias in node.names):
                hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] == "fcntl":
                hits.append(node.lineno)
    return hits


class TestFlockCompat:
    def test_posix_delegates_to_real_fcntl(self):
        # On this (POSIX) box the shim must expose the real fcntl constants.
        import fcntl

        from kiro_crew import flock_compat

        assert flock_compat.HAVE_FCNTL is True
        assert flock_compat.LOCK_EX == fcntl.LOCK_EX
        assert flock_compat.LOCK_SH == fcntl.LOCK_SH
        assert flock_compat.LOCK_UN == fcntl.LOCK_UN
        assert flock_compat.LOCK_NB == fcntl.LOCK_NB

    def test_windows_fallback_is_a_noop(self, monkeypatch):
        # Simulate Windows (no fcntl): the module must still import, HAVE_FCNTL
        # is False, flock is a harmless no-op, and constants are present — so the
        # CLI (and `kirocrew cloud` on Windows) can import the lock-using modules.
        sys.modules.pop("kiro_crew.flock_compat", None)
        real_import = builtins.__import__

        def _blocked(name, *a, **k):
            if name == "fcntl":
                raise ImportError("simulated Windows: no fcntl")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        try:
            fc = importlib.import_module("kiro_crew.flock_compat")
            assert fc.HAVE_FCNTL is False
            assert fc.flock(0, fc.LOCK_EX) is None  # no-op, no raise
            assert (fc.LOCK_EX, fc.LOCK_SH, fc.LOCK_UN, fc.LOCK_NB) == (2, 1, 8, 4)
            # ioctl is unavailable on Windows — must raise (loud), not silently
            # mis-behave (only the PTY handler uses it, and it never runs here).
            import pytest

            with pytest.raises(NotImplementedError):
                fc.ioctl(0, 0)
        finally:
            # Restore the real (POSIX) module for the rest of the suite.
            monkeypatch.undo()
            sys.modules.pop("kiro_crew.flock_compat", None)
            importlib.import_module("kiro_crew.flock_compat")

    def test_cli_import_graph_has_no_bare_fcntl_import(self):
        # Regression guard for the Windows cloud client: NO module reachable from
        # `kiro_crew.cli` at import time may import fcntl (POSIX-only)
        # unconditionally at module top level — they must go through flock_compat
        # or guard the import (``if IS_POSIX:`` / ``try:`` / inside a function),
        # which is how every existing POSIX-only import in the tree is written. A
        # future bare import would crash `python -m kiro_crew cloud launch` on
        # Windows before the handler runs.
        #
        # Run in a FRESH subprocess: the in-process sys.modules is polluted by
        # earlier tests (which may import off-CLI-path modules that legitimately
        # use fcntl), so we must measure the CLI graph in isolation. The scanner
        # is the same function the controls below exercise in-process.
        import inspect
        import subprocess
        import sys as _sys

        code = (
            "import ast, sys\n"
            + inspect.getsource(_unconditional_fcntl_imports)
            + "import kiro_crew.cli\n"  # populate ONLY the CLI import graph
            "bad = []\n"
            "for name, mod in list(sys.modules.items()):\n"
            "    if not name.startswith('kiro_crew'):\n"
            "        continue\n"
            "    path = getattr(mod, '__file__', None)\n"
            "    if not path or not path.endswith('.py'):\n"
            "        continue\n"
            "    try:\n"
            "        src = open(path, encoding='utf-8').read()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if _unconditional_fcntl_imports(src):\n"
            "        bad.append(name)\n"
            "print(','.join(bad))\n"
        )
        out = subprocess.run(
            [_sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, f"cli import failed:\n{out.stderr}"
        offenders = [m for m in out.stdout.strip().split(",") if m]
        assert not offenders, f"unconditional fcntl import on the CLI import path: {offenders}"

    def test_fcntl_import_scanner_detects_real_imports_and_ignores_inert_text(self):
        import pytest

        scan = _unconditional_fcntl_imports
        # Positive controls: both spellings of an unconditional top-level import
        # are what crashes a Windows import, and both are reported by line.
        assert scan("import fcntl\n") == [1]
        assert scan("import os\nimport fcntl as _fcntl\n") == [2]
        assert scan("from fcntl import flock\n") == [1]
        assert scan("from fcntl import LOCK_EX, LOCK_NB, flock\n") == [1]
        assert scan("import os, fcntl\n") == [1]
        # Negative controls: text that never executes as an import of this
        # module — a rendered Linux-only launcher template, a docstring, a
        # comment — and the guarded shapes the tree already uses.
        assert scan('TEMPLATE = """\nimport fcntl\n"""\n') == []
        assert scan('T = f"""\nfrom fcntl import flock\n{x}"""\n') == []
        assert scan('"""Usage: replace ``import fcntl``."""\n') == []
        assert scan("# import fcntl\nimport os\n") == []
        assert scan("if IS_POSIX:\n    import fcntl\n") == []
        assert (
            scan("try:\n    import fcntl as _fcntl\nexcept ImportError:\n    _fcntl = None\n") == []
        )
        assert scan("def f():\n    import fcntl\n    return fcntl\n") == []
        assert scan("from fcntl_compat import flock\n") == []
        assert scan("import fcntlx\n") == []
        # Unparseable text is reported rather than silently passed.
        with pytest.raises(SyntaxError):
            scan("import fcntl(\n")
