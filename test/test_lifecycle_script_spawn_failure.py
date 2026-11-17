"""A lifecycle script whose process cannot be started is a failed script.

``run_lifecycle_script`` promises a ``{"output", "failed"}`` result, and every
caller acts on ``failed``: enable rolls the app back, an update notes it, and
uninstall and teardown carry on. A spawn that raised instead (no shell, EAGAIN,
ENOMEM) skipped all of that: an enable was left half done and an uninstall
stopped after its crons were already removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps import lifecycle_scripts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory", "/bin/bash"),
        BlockingIOError(11, "Resource temporarily unavailable"),
    ],
)
async def test_a_spawn_that_fails_is_a_failed_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: OSError
) -> None:
    (tmp_path / "demo").mkdir()
    wrapper_file = tmp_path / "sandbox-profile"
    wrapper_file.write_text("profile")
    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", lambda argv, **k: (argv, str(wrapper_file)))
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)

    async def _spawn(*argv, **kwargs):
        raise error

    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)

    result = await lifecycle_scripts.run_lifecycle_script("demo", "echo hi", action="on_enable")

    assert result["failed"] is True
    assert "could not be started" in result["output"]
    # The sandbox wrapper's temp file is still cleaned up.
    assert not wrapper_file.exists()
