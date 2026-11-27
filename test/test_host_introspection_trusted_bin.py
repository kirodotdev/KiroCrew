"""No gateway spawn of a process or system-metrics tool takes its binary from PATH.

The gateway's ``PATH`` can lead with a directory a sandboxed agent can write: the
service unit puts ``~/.local/bin`` first (``service.common.service_path``). These
tools run in the gateway, outside the sandbox, on a dashboard metrics poll or an
orphan sweep, so a bare ``argv[0]`` would run whatever the agent last planted
under that name.

Every binary named here ships in a fixed system directory on the platforms that
spawn it, so :func:`kiro_crew.platform_compat.trusted_system_bin` resolves it on
a stock host and the pin costs nothing. The audit is mechanical for the reason
the desktop-UI audit beside it is: the rule is easy to state and easy to forget
at the next call site. The call-shape helpers are that audit's own.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from source_corpus import parsed_candidates, src_root
from test_desktop_ui_trusted_bin import _SPAWN_CALLS, _argv_head, _spawn_call_name

#: OS-shipped process and metrics tools the gateway itself spawns.
_HOST_INTROSPECTION_BINARIES = frozenset({"ps", "pgrep", "sysctl", "vm_stat", "netstat"})

#: Skill scripts are run by an agent, inside its sandbox, with the agent's own
#: ``PATH``. A bare name there resolves where the agent could already write, so
#: it gains nothing, and the gateway never executes them itself.
_AGENT_RUN_SCRIPT_ROOTS = ("builtin_skills/",)

pytestmark = pytest.mark.xdist_group(name="tree_scan_host_introspection_trusted_bin")


def _bare_binary_name(node: ast.expr | None) -> str | None:
    """The audited bare name a literal ``argv[0]`` spells, or None."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    value = node.value
    if "/" in value or "\\" in value:
        return None
    return value if value in _HOST_INTROSPECTION_BINARIES else None


def _violations() -> list[str]:
    """``<relpath>:<line> <binary>`` for every bare audited spawn the gateway runs."""
    needles = tuple(
        quote + name + quote for name in _HOST_INTROSPECTION_BINARIES for quote in ('"', "'")
    )
    root = src_root()
    found: list[str] = []
    for path, _text, tree in parsed_candidates(require_any=needles):
        rel = Path(path).relative_to(root).as_posix()
        if rel.startswith(_AGENT_RUN_SCRIPT_ROOTS):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _spawn_call_name(node) not in _SPAWN_CALLS:
                continue
            binary = _bare_binary_name(_argv_head(tree, node))
            if binary is not None:
                found.append(f"{rel}:{node.lineno} {binary}")
    return sorted(found)


def test_no_host_introspection_spawn_resolves_its_binary_off_path():
    """Each audited binary reaches the spawn as an absolute path.

    Resolve it with ``platform_compat.trusted_system_bin(name)``. There is no
    allowlist to add a site to: a bare-name fallback reinstates the hazard.
    """
    bare = _violations()
    assert not bare, (
        "Gateway spawns taking a process or metrics tool from PATH (resolve each "
        "through platform_compat.trusted_system_bin):\n  " + "\n  ".join(bare)
    )


def test_the_audit_sees_a_bare_spawn():
    """The predicate flags the shape it exists for, so a silent pass means clean."""
    tree = ast.parse(
        "import subprocess\n"
        "def f(pid):\n"
        "    cmd = ['pgrep', '-P', str(pid)]\n"
        "    subprocess.check_output(cmd)\n"
        "    subprocess.check_output(['/bin/ps', '-A'])\n"
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _spawn_call_name(node) == "check_output"
    ]
    assert [_bare_binary_name(_argv_head(tree, call)) for call in calls] == ["pgrep", None]


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="the orphan sweep is POSIX-only")
def test_the_orphan_sweep_runs_the_system_ps(monkeypatch: pytest.MonkeyPatch):
    """The macOS orphan scan spawns the system ``ps`` by absolute path, not by name."""
    from kiro_crew import platform_compat, session_pid

    spawned: list[list[str]] = []

    def _check_output(cmd: list[str], **_kwargs: object) -> bytes:
        spawned.append(cmd)
        return b"4242 1\n"

    # Shared names of the session_pid family are patched on its facade, which
    # reaches every owner that binds them.
    monkeypatch.setattr(session_pid, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(session_pid, "_accepted_subreaper_pids", lambda: {1})
    monkeypatch.setattr(
        session_pid, "subprocess", SimpleNamespace(check_output=_check_output, DEVNULL=-3)
    )
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/bin/{name}")
    assert session_pid._our_orphan_pids() == [4242]
    assert [cmd[0] for cmd in spawned] == ["/bin/ps"]
