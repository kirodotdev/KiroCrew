"""A fresh gateway start leaves nothing in the data home readable by another account.

Boots the real startup path -- the two data-home steps the ``kirocrew gateway``
CLI prologue runs (``ensure_data_home`` makes the home ``0700``,
``tighten_data_home`` sweeps it) followed by the real ``GatewayOrchestrator``
boot and shutdown -- on a fresh home under a ``022`` umask, then walks the
whole tree. Any file or directory a boot step creates without the owner-only
mode shows up here by name, which is the point: the sweep has already run when
the services start, so it cannot be what makes this pass for anything they
write. The one file the harness itself plants before the boot
(``config.local.json``, written ``0644``) is the sweep's to fix, and is checked.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from integration import conftest as harness

from kiro_crew.config.paths import config_dir, ensure_data_home
from kiro_crew.owner_only_files import tighten_data_home

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")


def _readable_by_others(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            # Not this policy's files, each for a stated reason:
            # * ``kiro/`` is this harness's KIRO_HOME (kiro-cli's own home, which
            #   production keeps at ``~/.kiro``, outside the data home);
            # * ``skills/`` is the installed skill trees, whose permission bits
            #   the builtin-skill sync fingerprints (owner_only_files
            #   .STARTUP_SWEEP_SKIPPED says why that is a separate change).
            dirnames[:] = [d for d in dirnames if d not in ("kiro", "skills")]
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue  # removed by shutdown while walking
            if stat.S_ISLNK(st.st_mode) or stat.S_ISSOCK(st.st_mode):
                continue
            if stat.S_IMODE(st.st_mode) & 0o077:
                found.append(f"{oct(stat.S_IMODE(st.st_mode))} {path.relative_to(root)}")
    return sorted(found)


@pytest.mark.asyncio
async def test_a_fresh_boot_leaves_nothing_readable_by_others(integration_home: Path) -> None:
    planted = integration_home / "config.local.json"
    assert stat.S_IMODE(planted.stat().st_mode) & 0o077, "the harness no longer plants a 0644 file"
    previous = os.umask(0o022)
    try:
        ensure_data_home()
        report = tighten_data_home(config_dir())
        assert report.complete, report
        async with harness.booted_gateway(integration_home) as gw:
            await gw.get_json("/api/health", auth=False)
            sessions = await gw.get("/api/sessions")
            assert sessions.status == 200, await sessions.text()
    finally:
        os.umask(previous)

    assert stat.S_IMODE(integration_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(planted.stat().st_mode) == 0o600
    assert _readable_by_others(integration_home) == []
    # The exclusions above must not hide the stores this boot creates.
    assert (integration_home / "sessions").is_dir() or (integration_home / "apps").is_dir()
