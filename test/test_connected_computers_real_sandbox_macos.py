"""Connected computers' actual macOS Seatbelt fence, on the macOS CI shard.

Mirrors ``test_connected_computers_real_sandbox.py``: the real ``self_test``
(control and confined probe, the Mach witness included) through the real
``sandbox-exec``, then an outside-root write and a loader variable that must
reach the child only inside the fence.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import pytest

from kiro_crew.connected_computers.confinement import (
    plan_for_this_machine,
    self_test,
)
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, default_timeout

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt confinement only"),
    pytest.mark.xdist_group(name="subprocess_spawn"),
]


def _run(confined) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
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


#: A fixed profile that proves only that this host's sandbox-exec can sandbox.
_REFERENCE_PROFILE = "(version 1)(allow default)"


@pytest.fixture(scope="module")
def mac_plan(tmp_path_factory):
    """The real plan, once its own wrap has run a trivial command.

    Only the host check may skip: sandbox-exec missing, or unable to run a fixed
    reference profile. Once the host can sandbox, a failure of the production
    profile or launcher is a failed test.
    """
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        pytest.skip("this Mac has no sandbox-exec")
    reference = subprocess.run(
        [sandbox_exec, "-p", _REFERENCE_PROFILE, "/usr/bin/true"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    if reference.returncode:
        pytest.skip(f"sandbox-exec cannot sandbox on this host: {reference.stderr.strip()}")
    root = tmp_path_factory.mktemp("capability-root")
    scratch = tmp_path_factory.mktemp("capability-scratch")
    plan = plan_for_this_machine()
    confined = plan.wrap(["/usr/bin/true"], root=str(root), scratch=str(scratch))
    result = _run(confined)
    assert result.returncode == 0, f"the production wrap failed: {result.stderr.strip()}"
    return plan


def test_real_macos_confinement_passes_self_test(tmp_path, mac_plan):
    root = tmp_path / "shared"
    root.mkdir()

    async def check():
        await asyncio.wait_for(
            self_test(mac_plan, str(root), timeout=default_timeout()),
            timeout=default_timeout() * 3,
        )

    # The real judge raises if the control or the confined probe disagrees.
    asyncio.run(check())
    assert list(root.iterdir()) == []


def test_real_macos_confinement_refuses_an_outside_write_and_keeps_env_inside(tmp_path, mac_plan):
    root = tmp_path / "shared"
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    for folder in (root, scratch, outside):
        folder.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    target = outside / "new-file"
    inside = root / "made-inside"
    confined = mac_plan.wrap(
        [
            sys.executable,
            "-I",
            "-c",
            "import os, sys; print(os.environ.get('KC_MARKER'), flush=True);"
            " open(sys.argv[2], 'w').write('ok');"
            " open(sys.argv[1], 'w').write('escaped')",
            str(target),
            str(inside),
        ],
        root=str(root),
        scratch=str(scratch),
        env={"KC_MARKER": "set-inside", "DYLD_INSERT_LIBRARIES": str(root / "x.dylib")},
    )
    # The wrapper's own environment carries neither the setting nor the loader variable.
    assert "KC_MARKER" not in confined.env
    assert "DYLD_INSERT_LIBRARIES" not in confined.env
    assert not any("set-inside" in arg for arg in confined.argv)
    result = _run(confined)
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == "set-inside", result.stdout + result.stderr
    assert inside.read_text(encoding="utf-8") == "ok"
    assert not target.exists()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    # The environment file was unlinked before the child ran.
    assert not [p for p in os.listdir(scratch) if p.startswith(".kirocrew-env-")]
