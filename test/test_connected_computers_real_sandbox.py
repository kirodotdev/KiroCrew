"""Connected computers' actual Linux bubblewrap/seccomp fence, on capable CI hosts."""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from kiro_crew.connected_computers.confinement import (
    ConfinementUnavailable,
    plan_for_this_machine,
    self_test,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, default_timeout

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux confinement only"
)


@pytest.fixture
def capable_plan(tmp_path):
    """Probe bubblewrap itself; its executable may have a distinct userns policy."""
    root = tmp_path / "capability-root"
    scratch = tmp_path / "capability-scratch"
    root.mkdir()
    scratch.mkdir()
    try:
        plan = plan_for_this_machine()
        confined = plan.wrap(["/usr/bin/true"], root=str(root), scratch=str(scratch))
    except ConfinementUnavailable as exc:
        pytest.skip(f"bubblewrap confinement unavailable: {exc}")
    try:
        result = subprocess.run(
            confined.argv,
            cwd=confined.cwd,
            env=confined.env,
            pass_fds=confined.pass_fds,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )
    finally:
        confined.close()
    if result.returncode:
        pytest.skip(f"bubblewrap cannot sandbox on this host: {result.stderr.strip()}")
    return plan


def test_real_linux_confinement_passes_self_test_and_refuses_outside_write(tmp_path, capable_plan):
    root = tmp_path / "shared"
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    root.mkdir()
    scratch.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    target = outside / "new-file"
    plan = capable_plan

    async def check():
        await asyncio.wait_for(
            self_test(plan, str(root), timeout=default_timeout()), timeout=default_timeout() * 3
        )

    # The real judge raises if either the unconfined control or confined probe disagrees.
    asyncio.run(check())
    assert list(root.iterdir()) == []

    confined = plan.wrap(
        [
            sys.executable,
            "-I",
            "-c",
            "import os, sys; print(os.environ.get('KC_MARKER'), flush=True);"
            " open(sys.argv[1], 'w').write('escaped')",
            str(target),
        ],
        root=str(root),
        scratch=str(scratch),
        env={"KC_MARKER": "set-inside"},
    )
    try:
        result = subprocess.run(
            confined.argv,
            cwd=confined.cwd,
            env=confined.env,
            pass_fds=confined.pass_fds,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=default_timeout(),
            check=False,
        )
    finally:
        confined.close()
    assert result.returncode != 0, result.stdout + result.stderr
    # The child's own settings reach it through bubblewrap's --args descriptor.
    assert result.stdout.splitlines()[0] == "set-inside", result.stdout + result.stderr
    assert "Traceback" in result.stderr and str(target) in result.stderr
    assert not target.exists()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
