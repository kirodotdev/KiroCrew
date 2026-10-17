"""A failed ``--list-models`` fetch is remembered for a short backoff window.

While kiro-cli was signed out, every ``/api/models`` poll spawned a fresh
``kiro-cli chat --list-models`` that exited 1 and logged one WARNING: the catalog
cache held only successes, so nothing stopped the next poll (81 spawns and 81
WARNING lines in 16 minutes on one gateway). These tests pin the negative entry:
polls inside ``_LIST_MODELS_FAILURE_BACKOFF_SECS`` answer the same 503 without a
spawn, a poll after it spawns again, and a success clears the entry.

The handler's clock is a fake injected on the module's ``time`` binding only, so
no test sleeps and the event loop keeps the real clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

_OK_PAYLOAD = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()


class _FakeClock:
    """``time`` stand-in for the handler module: a settable ``monotonic``."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


class _Proc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        # Above every platform's pid_max: no kill can reach a real process.
        self.pid = 99_999_999_999

    def kill(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _Spawner:
    """Counts spawns; each call returns the next scripted process."""

    def __init__(self) -> None:
        self.calls = 0
        self.next: _Proc = _signed_out()

    async def __call__(self, *a: Any, **k: Any) -> _Proc:
        del a, k
        self.calls += 1
        return self.next


def _signed_out() -> _Proc:
    return _Proc(b"", b"error: service error", 1)


def _signed_in() -> _Proc:
    return _Proc(_OK_PAYLOAD, b"", 0)


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _request(tmp_path: Path) -> MagicMock:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    request = MagicMock()
    # No live providers: entitlement narrowing fails open to the catalog.
    request.app = {"kiro_prerequisite_service": service}
    return request


@pytest.fixture
def harness(_floor_monkeypatch: pytest.MonkeyPatch):
    """A fresh catalog cache, a fake clock and a counting spawn for one test."""
    clock = _FakeClock()
    spawner = _Spawner()
    _floor_monkeypatch.setattr(agents, "_catalog_cache", agents._CatalogCache())
    _floor_monkeypatch.setattr(agents, "time", clock)
    _floor_monkeypatch.setattr(agents, "spawn_supervised_oneshot", spawner)
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="kiro"))
    patches = [
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch.object(agents, "wrap_argv", lambda argv, **kwargs: (argv, None)),
        patch.object(agents, "cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
        patch("kiro_crew.platform.redact_via_context", lambda text: text),
    ]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(clock=clock, spawner=spawner)
    finally:
        for p in reversed(patches):
            p.stop()


def _poll(tmp_path: Path):
    return asyncio.run(agents.api_models(_request(tmp_path)))


def _list_models_warnings(caplog: pytest.LogCaptureFixture) -> int:
    return sum(
        1
        for r in caplog.records
        if r.levelno == logging.WARNING and "--list-models exited" in r.getMessage()
    )


def test_two_polls_inside_the_window_spawn_once(harness, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=agents.logger.name)
    first = _poll(tmp_path)
    harness.clock.now += agents._LIST_MODELS_FAILURE_BACKOFF_SECS - 1
    second = _poll(tmp_path)

    assert harness.spawner.calls == 1
    assert (first.status, second.status) == (503, 503)
    # The held 503 still says why the list is missing.
    assert json.loads(first.body) == {"error": "model list command failed"}
    assert json.loads(second.body) == {"error": "model list command failed"}
    assert _list_models_warnings(caplog) == 1


def test_a_poll_after_the_window_spawns_again(harness, tmp_path):
    assert _poll(tmp_path).status == 503
    harness.clock.now += agents._LIST_MODELS_FAILURE_BACKOFF_SECS

    resp = _poll(tmp_path)

    assert harness.spawner.calls == 2
    assert resp.status == 503


def test_a_success_clears_the_negative_entry(harness, tmp_path):
    assert _poll(tmp_path).status == 503
    harness.clock.now += agents._LIST_MODELS_FAILURE_BACKOFF_SECS
    harness.spawner.next = _signed_in()

    ok = _poll(tmp_path)

    assert ok.status == 200
    assert [m["model_name"] for m in json.loads(ok.body)] == ["claude-opus-4.8"]
    assert agents._catalog_cache.failure is None
    # Served from the warm cache: no further spawn inside the old window.
    harness.clock.now += 1
    assert _poll(tmp_path).status == 200
    assert harness.spawner.calls == 2


def test_an_unresolved_binary_is_not_held(harness, tmp_path):
    # No spawn ran, and the binary resolves on its own shortly after start, so
    # the next poll retries instead of waiting out a window.
    with patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value=""):
        resp = _poll(tmp_path)
    assert json.loads(resp.body) == {"error": "kiro binary not resolved"}
    assert agents._catalog_cache.failure is None

    _poll(tmp_path)
    assert harness.spawner.calls == 1


def test_a_failed_refresh_of_a_stale_catalog_is_not_retried_inside_the_window(harness, tmp_path):
    harness.spawner.next = _signed_in()
    assert _poll(tmp_path).status == 200
    harness.clock.now += agents._LIST_MODELS_CATALOG_TTL_SECS
    harness.spawner.next = _signed_out()

    async def _stale_polls() -> list[int]:
        statuses = [(await agents.api_models(_request(tmp_path))).status]
        # Let the background refresh started by the stale poll land and fail.
        task = agents._catalog_cache.task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        statuses.append((await agents.api_models(_request(tmp_path))).status)
        return statuses

    # The last-good catalog is still served while the refresh is held back.
    assert asyncio.run(_stale_polls()) == [200, 200]
    assert harness.spawner.calls == 2
