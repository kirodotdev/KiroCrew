"""Tests for /api/models degraded-path handling (non-claude_code / kiro provider).

The model picker loads its list once via React Query and caches the result. A
successful (HTTP 200) empty list is cached as "there are zero models" and only a
manual page refresh re-fires the request. The common trigger was a slow cold
`kiro-cli --list-models` spawn: on timeout / spawn failure the handler used to
return `[]` with HTTP 200, so the picker rendered empty until refresh.

These tests pin the fix: every DEGRADED branch (binary unresolved, timeout,
unexpected exception) must return HTTP 503 so the frontend's fetch helper throws
and React Query retries with backoff, while a genuine successful parse stays 200.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_crew.dashboard.handlers import agents


def _kiro_request() -> MagicMock:
    return MagicMock()


def _kiro_cfg() -> SimpleNamespace:
    # Any non-"claude_code" provider takes the subprocess path under test.
    return SimpleNamespace(agent=SimpleNamespace(provider="kiro"))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _body(resp) -> object:
    return json.loads(resp.body)


class _FakeProc:
    """Minimal async subprocess stand-in for the timeout branch."""

    def __init__(self, stdout: bytes = b""):
        self._stdout = stdout

    def kill(self):  # noqa: D401 - matches Process API
        pass

    async def communicate(self):
        return self._stdout, b""


def test_kiro_binary_unresolved_returns_503():
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin", return_value=""
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert "error" in _body(resp)


def test_list_models_timeout_returns_503():
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", lambda argv: (argv, None)
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc()
    ), patch.object(
        agents.asyncio, "wait_for", side_effect=asyncio.TimeoutError
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert "error" in _body(resp)


def test_unexpected_exception_returns_503():
    # A failure inside the try (here: kiro-bin resolution raising) must be
    # caught and surfaced as 503, not a cached empty 200.
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin", side_effect=RuntimeError("boom")
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503


def test_successful_list_returns_200_with_models():
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8", "description": "x"}]}).encode()
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", lambda argv: (argv, None)
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ), patch.object(
        agents.asyncio, "wait_for", return_value=(payload, b"")
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 200
    models = _body(resp)
    assert any(m["model_name"] == "claude-opus-4.8" for m in models)
