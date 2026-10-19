"""The fixture and helper shared by ``test_mcp_discovery.py`` and its six topic files.

``_clear_cache`` empties the probe cache, and the autouse ``_passthrough_sandbox`` runs each
probed command unwrapped, so the tests exercise the probe's protocol handling and process
cleanup rather than which sandbox backend the host has. Each test file imports the autouse
fixture by name, which is what registers it there.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_discovery import _probe_cache


def _clear_cache() -> None:
    _probe_cache.clear()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse sandbox passthrough moved verbatim; no test in the test_mcp_discovery*.py modules calls monkeypatch.undo()
def _passthrough_sandbox(monkeypatch):
    """``probe_server`` routes the spawned MCP binary through
    ``sandboxed_spawn_argv`` → ``wrap_argv``, which raises when no OS-level
    sandbox backend is available (e.g. macOS without sandbox-exec). These tests
    exercise the probe's protocol handling and process cleanup, not sandbox
    availability, so run the command unwrapped in-test (preserving the caller's
    ``env=`` kwarg / falling back to os.environ)."""
    import os as _os

    def _passthrough(argv, *a, env=None, **k):
        return list(argv), dict(env if env is not None else _os.environ), None

    monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _passthrough)
