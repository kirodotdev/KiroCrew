"""Regression test for `_run_aim()` PATH augmentation.

The dashboard's `_run_aim` helper shells out to the `aim` CLI. When KiroCrew
runs under systemd or any other non-login shell, `~/.toolbox/bin` is often
missing from the inherited PATH, causing `aim` to fail with `[Errno 2] No
such file or directory: 'aim'`. The fix explicitly augments PATH via
`kiro_crew.env.augmented_path()` before spawning the subprocess.

Also verifies that the companion `_aim_path()` guard uses the same
augmentation, so handlers don't return 503 "aim CLI not found" under a
stripped systemd PATH when `aim` is actually installed in `~/.toolbox/bin`.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents as agents_handler


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"ok", b"")


@pytest.mark.asyncio
async def test_run_aim_augments_path():
    """`_run_aim` must pass an env whose PATH includes `~/.toolbox/bin`."""
    created = {}

    async def fake_exec(*args, **kwargs):
        created["args"] = args
        created["env"] = kwargs.get("env")
        return _FakeProc()

    with patch(
        "kiro_crew.dashboard.handlers.agents.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=fake_exec),
    ):
        rc, out = await agents_handler._run_aim("skills", "list")

    assert rc == 0
    assert out == "ok"
    env = created["env"]
    assert env is not None, "env must be passed so PATH augmentation applies"
    assert ".toolbox/bin" in env["PATH"], f"PATH missing ~/.toolbox/bin: {env['PATH']!r}"


def test_aim_path_finds_aim_in_toolbox_under_stripped_path(tmp_path, monkeypatch):
    """`_aim_path()` must locate `aim` via augmented PATH even when
    `os.environ["PATH"]` is stripped (systemd default).

    Mirrors the handler guard used by `/api/aim/mcp/registry` and siblings:
    the bare `shutil.which("aim")` call would return None under a stripped
    PATH and trip the 503 "aim CLI not found" branch, which is what
    caused the "Failed to load registry" dashboard error.
    """
    # Simulate aim installed in the Toolbox directory that augmented_path
    # prepends (`~/.toolbox/bin`).
    fake_home = tmp_path
    toolbox_bin = fake_home / ".toolbox" / "bin"
    toolbox_bin.mkdir(parents=True)
    fake_aim = toolbox_bin / "aim"
    fake_aim.write_text("#!/bin/sh\nexit 0\n")
    fake_aim.chmod(0o755)

    # Strip PATH (systemd-style) and point HOME at our fake toolbox.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(fake_home))
    # augmented_path uses os.path.expanduser which consults HOME.
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    found = agents_handler._aim_path()
    assert found == str(fake_aim), (
        f"expected _aim_path() to resolve {fake_aim}, got {found!r}"
    )


def test_aim_path_returns_none_when_missing(tmp_path, monkeypatch):
    """`_aim_path()` returns None when `aim` is not installed anywhere
    on the augmented PATH — the 503 branch is still reachable."""
    fake_home = tmp_path  # no aim binary anywhere
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    assert agents_handler._aim_path() is None
