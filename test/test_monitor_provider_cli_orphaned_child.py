"""A provider CLI's background child cannot outlive the monitor probe that started it.

``run_provider_cli`` waits for the CLI, then for its output pipes. When the CLI
exits but leaves a child holding those pipes, the probe stops the CLI's process
group. That group has to be signalled even though the CLI itself has exited, or
the child keeps running and the probe waits for it.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest

from kiro_crew import platform_compat
from kiro_crew.monitoring import provider_cli as provider_cli_module

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="process groups are POSIX; Windows stops the tree through its job object",
)


def _run_as_plain_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the probe as a plain shell: no sandbox, audit log or resource limits."""
    monkeypatch.setattr(provider_cli_module, "resolve_provider_cli", lambda executable: "/bin/sh")
    monkeypatch.setattr(provider_cli_module, "_audit_provider_cli", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        provider_cli_module,
        "provider_cli_env",
        lambda executable, credentials=None: {"PATH": "/usr/bin:/bin"},
    )
    monkeypatch.setattr(
        provider_cli_module,
        "sandboxed_spawn_argv",
        lambda args, **kwargs: (list(args), dict(kwargs["env"]), None),
    )
    monkeypatch.setattr(
        provider_cli_module, "finish_suspended_spawn", lambda proc, pid, label=None: True
    )

    @contextmanager
    def popen(args, creationflags=0, **kwargs):
        with subprocess.Popen(args, **kwargs) as proc:
            yield proc

    monkeypatch.setattr(provider_cli_module, "popen_limited", popen)


def test_a_background_child_holding_the_pipes_is_stopped_with_the_probe(monkeypatch, tmp_path):
    _run_as_plain_shell(monkeypatch)
    group_file = tmp_path / "group"
    finished = tmp_path / "finished"
    # The CLI records its group, starts a child that keeps both pipes open, and
    # exits. The child marks the moment it runs to the end; stopped with the
    # probe, it never gets there.
    script = f'echo $$ > "{group_file}"; (sleep 20; touch "{finished}") & exit 0'
    try:
        with pytest.raises(provider_cli_module.SetupError, match="pipes did not close"):
            provider_cli_module.run_provider_cli("glab", ["-c", script], timeout=5)
        assert not finished.exists(), "the probe waited for the background child to finish"
    finally:
        group = int(group_file.read_text()) if group_file.exists() else None
        if group is not None and platform_compat.pgroup_exists(group):
            platform_compat.kill_process_group(group, platform_compat.SIGKILL)
