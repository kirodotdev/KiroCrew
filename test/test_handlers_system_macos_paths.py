"""Tests for macOS absolute path resolution in handlers_system.py.

The path constants are resolved once, when the module is imported. These tests
never re-import it: a reload re-runs the whole module body in the SHARED module,
which re-creates every handler function (the copies ``kiro_crew.dashboard.handlers``
and ``session_memory`` imported by value stop matching), regenerates the in-memory
telemetry salt and leaves the faked command paths behind for the rest of the worker.
They call ``_resolve_tool`` with the arguments the module binds each constant with,
and fake the module's OWN ``shutil``/``sys``/``subprocess`` bindings, never the
stdlib attributes.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from kiro_crew.dashboard import handlers_system


def _module_scope_stores(tree: ast.Module, constant: str) -> int:
    """How many places at module scope, nested blocks included, store to *constant*."""
    count, stack = 0, list[ast.AST](tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # its own scope: a `global` rebind there is caught separately
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == constant:
            count += 1
        stack.extend(ast.iter_child_nodes(node))
    return count


def _resolver_args(constant: str) -> tuple[str, str]:
    """The ``(command, fallback)`` literals handlers_system resolves *constant* with."""
    tree = ast.parse(Path(handlers_system.__file__).read_text(encoding="utf-8"))
    bindings: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if constant in {getattr(target, "id", None) for target in targets}:
            bindings.append(value)
    assert len(bindings) == 1, f"{constant}: {len(bindings)} module-level bindings, want 1"
    stores = _module_scope_stores(tree, constant)
    assert stores == 1, f"{constant}: bound {stores} times at module scope, want 1"
    rebinds = [
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Global) and constant in n.names
    ]
    assert rebinds == [], f"{constant} is rebound through `global` at lines {rebinds}"
    (call,) = bindings
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), ast.dump(call)
    assert call.func.id == "_resolve_tool" and not call.keywords, ast.dump(call)
    name, fallback = (ast.literal_eval(arg) for arg in call.args)
    return name, fallback


def _fake_which(monkeypatch: pytest.MonkeyPatch, which: Callable[[str], str | None]) -> None:
    monkeypatch.setattr(handlers_system, "shutil", SimpleNamespace(which=which))


def _collect_as_darwin(
    monkeypatch: pytest.MonkeyPatch, fake_check_output: Callable[..., bytes]
) -> dict[str, Any]:
    """Run ``_collect_system_metrics`` down its darwin branch, on any host.

    The collector reads ``sys.platform`` and ``subprocess.check_output`` through the
    module's own bindings, so substituting those runs the darwin branch hermetically:
    no real ``sysctl``/``vm_stat``/``ps`` is spawned. Under xdist CPU saturation the
    CPU block's real ``ps -A -o %cpu`` (timeout=2) gets SIGKILLed and the Popen-cleanup
    ``waitpid`` reap hangs past pytest-timeout.
    """
    monkeypatch.setattr(handlers_system, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        handlers_system,
        "subprocess",
        SimpleNamespace(check_output=fake_check_output, DEVNULL=subprocess.DEVNULL),
    )
    monkeypatch.setattr(handlers_system, "_get_static_system_info", lambda: {})
    # _local_ip() opens a UDP socket to 8.8.8.8:80 to learn the host's outbound
    # address: a real network dependency the Linux siblings already stub.
    monkeypatch.setattr(handlers_system, "_local_ip", lambda: "127.0.0.1")
    # darwin has no /proc/stat, so this probe answers None there and the collector
    # falls back to the (faked) ps; stubbed so a Linux host's real /proc/stat stays out.
    monkeypatch.setattr(handlers_system, "_system_cpu_pct_from_proc_stat", lambda: None)
    monkeypatch.setattr(handlers_system, "model_file_present", lambda: False)
    monkeypatch.setattr(
        handlers_system, "get_shared_embedder", lambda: SimpleNamespace(is_ready=lambda: False)
    )
    # The collector rebinds or mutates these in place: fresh values for this test, and
    # the originals back at teardown.
    monkeypatch.setattr(handlers_system, "_prev_net", {"rx": 0.0, "tx": 0.0, "ts": 0.0})
    monkeypatch.setattr(handlers_system, "_net_speed", {"rx_kbs": 0.0, "tx_kbs": 0.0})
    monkeypatch.setattr(handlers_system, "_prev_cpu", {"total": 0.0, "ts": 0.0})
    monkeypatch.setattr(handlers_system, "_proc_cpu_pct", 0.0)
    monkeypatch.setattr(handlers_system, "_proc_scan_cache", {})
    monkeypatch.setattr(handlers_system, "_proc_scan_cache_ts", 0.0)
    monkeypatch.setattr(
        handlers_system, "_live_mem_probe_reported", handlers_system._live_mem_probe_reported
    )
    return handlers_system._collect_system_metrics()


def _fake_trusted(monkeypatch: pytest.MonkeyPatch, lookup: Callable[[str], str | None]) -> None:
    monkeypatch.setattr(
        handlers_system,
        "platform_compat",
        SimpleNamespace(trusted_system_bin_quiet=lookup),
    )


_TOOL_CONSTANTS = ("_SYSCTL", "_VM_STAT", "_NETSTAT", "_PS")


class TestMacOsSysctlPaths:
    """Verify the system command paths resolve from the system directories only."""

    def test_a_path_hit_does_not_choose_the_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``sysctl`` first on PATH (say ``~/.local/bin``) is never the one that runs."""
        _fake_which(monkeypatch, lambda cmd: f"/home/u/.local/bin/{cmd}")
        _fake_trusted(monkeypatch, lambda cmd: f"/usr/sbin/{cmd}" if cmd == "sysctl" else None)
        assert handlers_system._resolve_tool(*_resolver_args("_SYSCTL")) == "/usr/sbin/sysctl"

    @pytest.mark.parametrize("constant", _TOOL_CONSTANTS)
    def test_each_tool_ignores_path(self, monkeypatch: pytest.MonkeyPatch, constant: str) -> None:
        """With no system copy, every tool takes its fixed fallback, never the PATH hit."""
        _fake_which(monkeypatch, lambda cmd: f"/home/u/.local/bin/{cmd}")
        _fake_trusted(monkeypatch, lambda cmd: None)
        name, fallback = _resolver_args(constant)
        assert handlers_system._resolve_tool(name, fallback) == fallback

    def test_every_tool_path_is_bound_through_the_resolver(self) -> None:
        """Each path constant is ``_resolve_tool(<its command>, <its fallback>)``."""
        assert {c: _resolver_args(c) for c in _TOOL_CONSTANTS} == {
            "_SYSCTL": ("sysctl", "/usr/sbin/sysctl"),
            "_VM_STAT": ("vm_stat", "/usr/bin/vm_stat"),
            "_NETSTAT": ("netstat", "/usr/sbin/netstat"),
            "_PS": ("ps", "/bin/ps"),
        }

    def test_without_a_system_pgrep_the_child_count_spawns_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_PGREP`` has no fixed fallback: with none, no pgrep runs and the count is 0."""
        spawned: list[str] = []

        def fake_check_output(cmd: list[str], **kwargs: Any) -> bytes:
            spawned.append(cmd[0])
            return b"%CPU\n0.0\n"

        monkeypatch.setattr(handlers_system, "_PGREP", None)
        data = _collect_as_darwin(monkeypatch, fake_check_output)
        assert data["child_processes"] == 0
        assert None not in spawned

    def test_collect_metrics_returns_mem_on_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On macOS, _collect_system_metrics returns mem_used_gb when commands succeed.

        Subprocess output is faked per command so the test is hermetic.
        """
        vm_stat_out = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                          100000.\n"
            "Pages inactive:                       50000.\n"
            "Anonymous pages:                     200000.\n"
            "Pages purgeable:                      10000.\n"
            "Pages wired down:                     80000.\n"
            "Pages occupied by compressor:         60000.\n"
        )

        def fake_check_output(cmd: list[str], **kwargs: Any) -> bytes:
            exe = cmd[0]
            if exe == handlers_system._SYSCTL:
                return b"34359738368"  # 32 GiB, hw.memsize
            if exe == handlers_system._VM_STAT:
                return vm_stat_out.encode()
            return b"%CPU\n0.0\n"  # ps -A -o %cpu and any other call

        data = _collect_as_darwin(monkeypatch, fake_check_output)

        assert "mem_total_gb" in data
        assert "mem_used_gb" in data
        assert data["mem_total_gb"] > 0
        assert data["mem_used_gb"] > 0

    def test_collect_metrics_legacy_vm_stat_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy vm_stat without 'Anonymous pages' falls back to 'Pages active'."""
        # Legacy output: only Pages free/inactive/active — no Anonymous pages line.
        vm_stat_out = (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "Pages free:                          100000.\n"
            "Pages inactive:                       50000.\n"
            "Pages active:                        300000.\n"
            "Pages wired down:                     80000.\n"
        )

        def fake_check_output(cmd: list[str], **kwargs: Any) -> bytes:
            exe = cmd[0]
            if exe == handlers_system._SYSCTL:
                return b"17179869184"  # 16 GiB
            if exe == handlers_system._VM_STAT:
                return vm_stat_out.encode()
            return b"%CPU\n0.0\n"

        data = _collect_as_darwin(monkeypatch, fake_check_output)

        assert "mem_used_gb" in data
        # Legacy fallback: app_pages = Pages active (300000), wired = 80000
        # used_bytes = (300000 + 80000) * 4096 = 1,556,480,000 ~ 1.4 GB > 0
        assert data["mem_used_gb"] > 0
