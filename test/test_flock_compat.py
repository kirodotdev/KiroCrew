"""Tests for the cross-platform flock shim (enables the Windows cloud client)."""

from __future__ import annotations

import builtins
import importlib
import sys


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
        # `kiro_crew.cli` at import time may do a bare `import fcntl` (POSIX-only)
        # — they must go through flock_compat. A future bare import would crash
        # `python -m kiro_crew cloud launch` on Windows before the handler runs.
        #
        # Run in a FRESH subprocess: the in-process sys.modules is polluted by
        # earlier tests (which may import off-CLI-path modules that legitimately
        # use fcntl), so we must measure the CLI graph in isolation.
        import subprocess
        import sys as _sys

        code = (
            "import sys, re\n"
            "import kiro_crew.cli\n"  # populate ONLY the CLI import graph
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
            "    if re.search(r'^import fcntl\\b', src, re.M):\n"
            "        bad.append(name)\n"
            "print(','.join(bad))\n"
        )
        out = subprocess.run(
            [_sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, f"cli import failed:\n{out.stderr}"
        offenders = [m for m in out.stdout.strip().split(",") if m]
        assert not offenders, f"bare 'import fcntl' on the CLI import path: {offenders}"
