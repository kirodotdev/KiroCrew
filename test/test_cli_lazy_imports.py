"""Regression guards for issue #3504: cli.py's module-scope import weight.

``cli.py`` used to import ``cli_commands`` (~556 ms), ``cli_server`` (~549 ms,
pulling ``slack.gateway``) and ``dashboard.state`` (pulling ``vector_memory``
→ ``numpy``, ~56 MB) at module scope, so every CLI invocation and — worse —
every long-lived MCP stdio server (``kirocrew mcp-core`` / ``mcp-cron`` /
``mcp-computer``) paid ~1.3 s and ~112 MB for subcommands that never run.
Those imports were moved into the one ``main()`` dispatch branch that uses
each name, cutting a fresh ``import kiro_crew.cli`` to ~0.5 s / ~54 MB.

The tests here keep it that way:

1. **Ratchet** — a fresh interpreter that imports ``kiro_crew.cli`` must not
   end up with any of the heavy modules in ``sys.modules``.  Without this, the
   next module-scope import silently undoes the win (the two historical
   ``# noqa: E402`` blocks show that already happened once).
2. **Dispatch integrity** — every function-local ``from kiro_crew.* import``
   inside ``main()`` must resolve.  A typo in a moved import is otherwise only
   discovered at runtime by the user who runs that one subcommand.
3. **Stdio servers still serve** — ``mcp-core`` / ``mcp-cron`` answer
   ``initialize`` + ``tools/list`` over stdio end-to-end through the real CLI
   entry point.
4. **Security prelude ordering** — ``boot_platform`` still runs before the
   mcp-core dispatch branch, and a ``PlatformCompositionError`` still aborts
   rather than downgrading.

Fresh-interpreter checks run in subprocesses: the warm test process has long
since imported the heavy modules, so an in-process ``sys.modules`` assertion
would be vacuous.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import cli
from kiro_crew.platform import PlatformCompositionError

_SRC = Path(__file__).resolve().parent.parent / "src"
_CLI_PY = _SRC / "kiro_crew" / "cli.py"

#: Modules that must NOT load as a side effect of ``import kiro_crew.cli``.
#: Each entry names a measured cost: cli_commands/cli_server are the two
#: deferred dispatch-table modules (~1.1 s combined), slack.gateway is
#: cli_server's heaviest edge, dashboard.state → vector_memory → numpy is the
#: ~56 MB RSS chain.
_BANNED_AFTER_CLI_IMPORT = (
    "kiro_crew.cli_commands",
    "kiro_crew.cli_server",
    "kiro_crew.slack.gateway",
    "kiro_crew.dashboard.state",
    "kiro_crew.vector_memory",
    "numpy",
)


def test_cli_import_does_not_load_heavy_modules() -> None:
    """Ratchet: ``import kiro_crew.cli`` must stay free of the deferred modules.

    If this fails, a module-scope import (direct or transitive) reached one of
    the banned modules again — move it into the dispatch branch that needs it
    instead of deleting it from the list.
    """
    code = (
        "import sys; "
        "import kiro_crew.cli; "
        "banned = " + repr(list(_BANNED_AFTER_CLI_IMPORT)) + "; "
        "present = [m for m in banned if m in sys.modules]; "
        "print(repr(present))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, f"import kiro_crew.cli failed:\n{res.stderr}"
    present = ast.literal_eval(res.stdout.strip())
    assert present == [], (
        f"module-scope import of kiro_crew.cli loaded deferred modules: {present}. "
        "A new (or moved-back) module-scope import reaches them — defer it into "
        "the main() dispatch branch that uses it (see issue #3504)."
    )


def _local_imports_in_main() -> list[tuple[str, str]]:
    """(module, name) for every function-local kiro_crew import inside main()."""
    tree = ast.parse(_CLI_PY.read_text(encoding="utf-8"))
    main_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    found: list[tuple[str, str]] = []
    for node in ast.walk(main_fn):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "kiro_crew" or node.module.startswith("kiro_crew.")
        ):
            for alias in node.names:
                found.append((node.module, alias.name))
    return found


def test_main_contains_the_deferred_dispatch_imports() -> None:
    """The dispatch imports really are function-local (not silently hoisted back)."""
    local = _local_imports_in_main()
    modules = {mod for mod, _ in local}
    assert "kiro_crew.cli_commands" in modules
    assert "kiro_crew.cli_server" in modules
    assert "kiro_crew.dashboard.state" in modules
    # Coherence check: the dispatch chain defers a substantial name set, not a remnant.
    assert len(local) >= 25, f"expected >=25 deferred imports in main(), found {len(local)}"


@pytest.mark.parametrize(
    "module,name",
    _local_imports_in_main(),
    ids=lambda v: v if isinstance(v, str) else repr(v),
)
def test_every_deferred_dispatch_import_resolves(module: str, name: str) -> None:
    """Each ``from <module> import <name>`` inside main() resolves.

    This is the compile-time check the deferral gave up: a typo in a moved
    import would otherwise surface only when a user runs that subcommand.
    """
    mod = importlib.import_module(module)
    assert hasattr(mod, name), f"main() imports {name} from {module}, which lacks it"


def _stdio_roundtrip(subcommand: str, tmp_path: Path) -> list[str]:
    """Start ``kirocrew <subcommand>`` over stdio; return the tools/list names."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "kiro_crew", subcommand],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "KIROCREW_HOME": str(tmp_path / "home"),
        },
    )
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-cli-lazy-imports", "version": "0"},
            },
        }
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"{subcommand}: no initialize response (stderr: {proc.stderr.read()[:500]})"
        resp = json.loads(line)
        assert resp.get("id") == 1 and "result" in resp, f"bad initialize response: {resp}"
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        proc.stdin.flush()
        line2 = proc.stdout.readline()
        assert line2, f"{subcommand}: no tools/list response"
        resp2 = json.loads(line2)
        assert resp2.get("id") == 2 and "result" in resp2, f"bad tools/list response: {resp2}"
        return [t["name"] for t in resp2["result"]["tools"]]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.parametrize("subcommand", ["mcp-core", "mcp-cron"])
def test_mcp_stdio_server_answers_initialize_and_tools_list(
    subcommand: str, tmp_path: Path
) -> None:
    """The stdio servers still boot and serve through the real CLI dispatch."""
    tools = _stdio_roundtrip(subcommand, tmp_path)
    assert len(tools) > 0, f"{subcommand} returned an empty tool table"


def test_boot_platform_runs_before_mcp_core_dispatch(monkeypatch) -> None:
    """Security prelude ordering survives the deferral.

    ``boot_platform`` must fire before the mcp-core branch imports and starts
    the server — the fail-closed contract is that a non-standalone profile
    with no security overlay never reaches ``run_mcp_core_server``.
    """
    order: list[str] = []

    monkeypatch.setattr(
        cli, "boot_platform", lambda *_a, **_k: order.append("boot_platform")
    )

    fake_mcp_core = types.ModuleType("kiro_crew.mcp_core")
    fake_mcp_core.run_mcp_core_server = lambda: order.append("dispatch")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew.mcp_core", fake_mcp_core)

    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "mcp-core"])
    cli.main()

    assert order == ["boot_platform", "dispatch"]


def test_platform_composition_error_still_aborts_mcp_core(monkeypatch) -> None:
    """A composition failure aborts BEFORE dispatch — no silent downgrade."""

    def _raise(*_a, **_k):
        raise PlatformCompositionError("companion missing")

    monkeypatch.setattr(cli, "boot_platform", _raise)

    dispatched: list[bool] = []
    fake_mcp_core = types.ModuleType("kiro_crew.mcp_core")
    fake_mcp_core.run_mcp_core_server = lambda: dispatched.append(True)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew.mcp_core", fake_mcp_core)

    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "mcp-core"])
    with pytest.raises(PlatformCompositionError):
        cli.main()
    assert dispatched == [], "mcp-core dispatched despite a failed platform composition"
