"""Fixtures and doubles shared by ``test_dev_fleet_server_coverage.py`` and its siblings.

``_pin_module_state`` is autouse, so every ``test_dev_fleet_server_coverage*.py``
module imports it by name: it resets the Dev Fleet modules' cached globals so no
test sees another's state. ``_sel_capture`` records each SEL audit call and
``_json_request`` builds the request double the audit decorator reads, both for
``test_dev_fleet_server_coverage_http_api.py`` and
``test_dev_fleet_server_coverage_untracked_discard.py``; ``_same`` compares paths
through symlinks and short names for ``test_dev_fleet_server_coverage.py`` and
``test_dev_fleet_server_coverage_lifecycle.py``. A helper only one module reads
stays in that module.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, repository, runtime, worktree_ops


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _CapturingSel:
    """Minimal SEL stand-in that records every audit call."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_tool_invocation(self, **kw) -> None:
        self.events.append(kw)


def _sel_capture(monkeypatch) -> _CapturingSel:
    sink = _CapturingSel()
    monkeypatch.setattr(runtime, "_sel", lambda: sink)
    return sink


def _json_request(payload: dict) -> MagicMock:
    """A request double shaped like the one the audit decorator expects."""
    raw = json.dumps(payload).encode()
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    request.json = AsyncMock(return_value=payload)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


def _same(a: str, b: str) -> bool:
    """Path equality that survives Windows 8.3 short paths and /tmp symlinks."""
    return os.path.realpath(a) == os.path.realpath(b)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in the modules that import it calls monkeypatch.undo()
def _pin_module_state(monkeypatch):
    """Neutralise the module's cached globals so tests never share state."""
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(fleet_state, "_HTML_BASE", None)
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(fleet_state, "_PROVISION_INFLIGHT", {})
    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_BUILD_PATH_CACHE", "/usr/bin")
    monkeypatch.setattr(runtime, "_warm_build_path", AsyncMock())
    monkeypatch.setattr(worktree_ops, "_pod_env", lambda: {})
    # Shutdown admission state: each test starts with a clean (non-shutdown) process.
    monkeypatch.setattr(runtime, "_SHUTDOWN_IN_PROGRESS", False)
    monkeypatch.setattr(runtime, "_SHUTDOWN_ADMISSION_LOCK", asyncio.Lock())
