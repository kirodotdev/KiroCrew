"""Subprocess smoke test for supervised-mode boot (contract C1).

Boots a real ``kirocrew gateway --supervised`` twice against the SAME populated
KIROCREW_HOME and asserts:

* READY prints in well under the READY budget on BOTH boots (the wedge that
  Phase 1 hit — a second boot on a populated home parking on ``await
  setup_tunnel`` and never printing READY — is exactly what supervised mode
  fixes by forcing publishing off);
* after SIGTERM the whole process group is gone — no orphaned children (e.g. the
  mcp-gateway daemon, which supervised mode does not start at all).

POSIX only: it relies on ``start_new_session`` + ``killpg`` group semantics. The
child is spawned in its own throwaway home, never the operator's; nothing here
touches a live gateway.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.testing.harness import (
    READY_PREFIX,
    parse_ready_line,
    terminate_pgid,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="supervised boot smoke test uses POSIX process groups"
)

# Generous ceiling for a real subprocess boot on a shared CI host; the contract
# target is < 10 s of gateway-internal READY time, asserted separately below on
# the parsed elapsed. This is only the outer read deadline.
_READ_TIMEOUT_SECS = 40.0
# Contract C1 READY target on a populated home.
_READY_BUDGET_SECS = 10.0


def _repo_src() -> Path:
    # <repo>/test/<thisfile> -> <repo>/src
    return Path(__file__).resolve().parent.parent / "src"


def _boot_env(home: Path) -> dict[str, str]:
    src = _repo_src()
    return {
        **os.environ,
        "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "KIROCREW_HOME": str(home),
        # Isolate the agent-spec home too (see harness.spawn_feature_gateway).
        "KIRO_HOME": str(home / "kiro"),
        "PYTHONUNBUFFERED": "1",
        # Never let a throwaway gateway kick the ~610MB embedding-model download.
        "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
        # Belt-and-suspenders: exercise the env path to enabling supervised mode
        # in addition to the flag below, so the OR is covered end to end.
        "KIROCREW_SUPERVISED": "1",
    }


def _spawn(home: Path, *, seed: bool) -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "kiro_crew",
        "gateway",
        "--supervised",
        "--port",
        "auto",
        "--no-open",
        "--json-ready",
        "--no-crons",
    ]
    if seed:
        cmd += ["--seed", "minimal"]
    return subprocess.Popen(
        cmd,
        env=_boot_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group, so killpg reaps the tree
    )


def _read_ready(proc: subprocess.Popen[bytes]) -> tuple[dict, float, str]:
    """Return (ready_payload, elapsed_secs, stderr_tail).

    Reads stdout on a thread so a silent-but-alive child still hits the
    deadline. Raises AssertionError with the stderr tail on early exit/timeout.
    """
    start = time.monotonic()
    q: "queue.Queue[bytes | None]" = queue.Queue()
    err: list[bytes] = []

    def _pump(stream, sink):
        try:
            for line in iter(stream.readline, b""):
                sink(line)
        finally:
            if sink is q.put:
                q.put(None)

    assert proc.stdout is not None and proc.stderr is not None
    threading.Thread(target=_pump, args=(proc.stdout, q.put), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err.append), daemon=True).start()

    deadline = start + _READ_TIMEOUT_SECS
    while True:
        if time.monotonic() > deadline:
            raise AssertionError(
                f"no {READY_PREFIX} within {_READ_TIMEOUT_SECS}s\n"
                f"stderr tail:\n{b''.join(err).decode(errors='replace')[-3000:]}"
            )
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                raise AssertionError(
                    f"gateway exited ({proc.returncode}) before {READY_PREFIX}\n"
                    f"stderr tail:\n{b''.join(err).decode(errors='replace')[-3000:]}"
                )
            continue
        if line is None:
            if proc.poll() is not None:
                raise AssertionError(
                    f"gateway exited ({proc.returncode}) before {READY_PREFIX}\n"
                    f"stderr tail:\n{b''.join(err).decode(errors='replace')[-3000:]}"
                )
            continue
        text = line.decode(errors="replace").strip()
        if text.startswith(READY_PREFIX):
            elapsed = time.monotonic() - start
            return parse_ready_line(text), elapsed, ""


@pytest.mark.timeout(180)
def test_supervised_boots_twice_ready_and_reaps():
    home = Path(tempfile.mkdtemp(prefix="kirocrew-sup-smoke-"))
    try:
        # ── Boot 1: seed + populate ──────────────────────────────────────
        p1 = _spawn(home, seed=True)
        try:
            payload1, elapsed1, _ = _read_ready(p1)
            assert payload1.get("port")
            assert elapsed1 < _READY_BUDGET_SECS, (
                f"boot 1 READY took {elapsed1:.1f}s (budget {_READY_BUDGET_SECS}s)"
            )
        finally:
            terminate_pgid(p1.pid, pgid=p1.pid, wait=p1.wait)
            p1.wait(timeout=10)
        # The group must be gone after SIGTERM — no orphaned children.
        from kiro_crew import platform_compat

        assert not platform_compat.pgroup_exists(p1.pid), (
            "boot 1 left a surviving process in its group after SIGTERM"
        )

        # ── Boot 2: SAME populated home, no re-seed ──────────────────────
        # This is the boot Phase 1 saw wedge. It must reach READY just as fast.
        p2 = _spawn(home, seed=False)
        try:
            payload2, elapsed2, _ = _read_ready(p2)
            assert payload2.get("port")
            assert elapsed2 < _READY_BUDGET_SECS, (
                f"boot 2 (populated home) READY took {elapsed2:.1f}s "
                f"(budget {_READY_BUDGET_SECS}s) — the wedge is back"
            )
        finally:
            terminate_pgid(p2.pid, pgid=p2.pid, wait=p2.wait)
            p2.wait(timeout=10)
        assert not platform_compat.pgroup_exists(p2.pid), (
            "boot 2 left a surviving process in its group after SIGTERM"
        )
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)
