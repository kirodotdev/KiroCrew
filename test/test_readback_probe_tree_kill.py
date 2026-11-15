"""A harness probe that times out ends everything it started.

The pi and opencode read-backs, and the version probes beside them, run the harness
(or the Node it runs under) once before a chat starts. When the
probe outlives its timeout, the kill must take the probe's descendants too, not only
the probe. Each test runs a real stand-in that starts a long-lived grandchild, and
forces the timeout only once both pids are on disk, so the outcome never depends on
how fast the runner is.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from spawn_test_helpers import LONG_CHILD_WITH_GRANDCHILD, await_gone, await_pids, kill_leftovers

from kiro_crew import platform_compat
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.harness import opencode as opencode_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness import pi_node as pi_node_mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE, ACP_BACKEND_PI
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: How long the forced timeout gives the probe once its pids are written.
_FORCED_TIMEOUT_SECS = 0.01


def _times_out_once_started(pidfile: Path) -> type[subprocess.Popen[Any]]:
    """A real ``Popen`` whose first timed ``communicate`` times out after *pidfile* exists.

    Only the first timed ``communicate`` of the whole test is forced: on Windows the
    tree kill itself runs ``taskkill`` through ``subprocess.run``, and that child must
    run normally.
    """
    forced = [False]

    class _Popen(subprocess.Popen):  # type: ignore[type-arg]
        def communicate(self, input: Any = None, timeout: float | None = None) -> Any:
            if timeout is not None and not forced[0]:
                forced[0] = True
                await_pids(pidfile)
                timeout = _FORCED_TIMEOUT_SECS
            return super().communicate(input, timeout)

    return _Popen


def _client(tmp_path: Path, backend: str) -> Any:
    return client_mod.AcpClient(work_dir=tmp_path, acp_backend=backend)


def _run_helper(tmp_path: Path, argv: list[str]) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        platform_compat.run_killing_tree_on_timeout(
            argv,
            capture_output=True,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            timeout=FALLBACK_TIMEOUT_SECS,
        )


def _run_pi(tmp_path: Path, argv: list[str]) -> None:
    issue, _remedy = pi_mod._verify_pi_gate(
        _client(tmp_path, ACP_BACKEND_PI), ACP_BACKEND_PI, argv, "/x"
    )
    assert "could not be read back" in issue


def _run_opencode(tmp_path: Path, argv: list[str]) -> None:
    issue, _remedy = opencode_mod._verify_opencode_routing(
        _client(tmp_path, ACP_BACKEND_OPENCODE), ACP_BACKEND_OPENCODE, argv, "{}"
    )
    assert "could not be read back" in issue


def _run_opencode_version(tmp_path: Path, argv: list[str]) -> None:
    client = _client(tmp_path, ACP_BACKEND_OPENCODE)
    assert opencode_mod._opencode_reported_major(client, argv) is None


def _run_node_version(tmp_path: Path, argv: list[str]) -> None:
    # ``node --version`` is spelled by the probe itself, so the stand-in is a
    # script named ``node`` that ignores its argument and runs *argv*.
    node = tmp_path / "node"
    node.write_text(f"#!/bin/sh\nexec {shlex.join(argv)}\n", encoding="utf-8")
    node.chmod(0o755)
    assert pi_node_mod._node_version(str(node), os.environ.get("PATH", ""), str(tmp_path)) is None


@pytest.mark.parametrize(
    "probe",
    [
        _run_helper,
        _run_pi,
        _run_opencode,
        _run_opencode_version,
        pytest.param(
            _run_node_version,
            marks=pytest.mark.skipif(not platform_compat.IS_POSIX, reason="a /bin/sh stand-in"),
        ),
    ],
    ids=["run_killing_tree_on_timeout", "pi", "opencode", "opencode-version", "node-version"],
)
def test_a_timed_out_probe_is_killed_with_everything_it_started(
    probe: Callable[[Path, list[str]], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pids"
    monkeypatch.setattr(subprocess, "Popen", _times_out_once_started(pidfile))
    pids: tuple[int, ...] = ()
    try:
        probe(tmp_path, [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)])
        pids = await_pids(pidfile)
        alive = [pid for pid in pids if not await_gone(pid)]
        assert alive == [], f"the probe's timeout left {alive} of {pids} running"
    finally:
        kill_leftovers(pids or (await_pids(pidfile) if pidfile.exists() else ()))
