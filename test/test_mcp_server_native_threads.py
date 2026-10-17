"""The managed MCP stdio servers stay off numpy and off a per-core BLAS pool.

A gateway runs one ``kirocrew mcp-core`` / ``mcp-cron`` / ``mcp-dashboard``
process per agent session. When any of them imports numpy, OpenBLAS starts one
spinning thread per core (64 on a large host) and the start costs several
CPU-seconds, for servers that never do array maths.

Two guards, each tested here:

- importing a server's entry module must not import numpy. The known chains are
  ``dashboard.stt_stream`` -> ``stt.engine`` / ``stt.vad`` (the local-recogniser
  session imports them itself) and ``mcp_core`` -> ``knowledge.retrieval`` (its
  numpy scorers import numpy on first use);
- every ``kirocrew mcp-*`` command caps OpenBLAS/OpenMP at one thread before it
  imports its server, unless the operator already set the variable, so a future
  import that reaches numpy again costs one thread rather than one per core.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import cli

_BLAS_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")


@pytest.mark.parametrize(
    "module",
    ["kiro_crew.mcp_dashboard", "kiro_crew.mcp_core", "kiro_crew.mcp_cron"],
)
def test_importing_mcp_server_does_not_load_numpy(module: str, tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    code = (
        "import sys, importlib; "
        f"importlib.import_module({module!r}); "
        "print(repr([m for m in ('numpy', 'kiro_crew.stt.engine', 'kiro_crew.stt.vad') "
        "if m in sys.modules]))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
    )
    assert res.returncode == 0, f"import {module} failed:\n{res.stderr}"
    present = ast.literal_eval(res.stdout.strip().splitlines()[-1])
    assert present == [], (
        f"import {module} loaded {present}; a module-scope import reaches numpy. "
        "Defer it into the function that needs it."
    )


def _run_mcp_core(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """Dispatch ``kirocrew mcp-core`` to a stub server; return the BLAS env it saw."""
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(cli, "boot_platform", lambda *_a, **_k: None)
    fake = types.ModuleType("kiro_crew.mcp_core")
    fake.run_mcp_core_server = lambda: seen.update({v: os.environ.get(v) for v in _BLAS_VARS})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew.mcp_core", fake)
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "mcp-core"])
    cli.main()
    return seen


def test_mcp_server_caps_blas_threads_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _BLAS_VARS:
        # setenv first so monkeypatch records the original state and removes
        # the value cli.main() sets when the test ends.
        monkeypatch.setenv(var, "x")
        monkeypatch.delenv(var)
    assert _run_mcp_core(monkeypatch) == {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}


def test_mcp_server_keeps_operator_thread_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "8")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    assert _run_mcp_core(monkeypatch) == {"OPENBLAS_NUM_THREADS": "8", "OMP_NUM_THREADS": "4"}
