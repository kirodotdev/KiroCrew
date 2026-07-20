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


async def _raise_timeout(awaitable, timeout):
    del timeout
    awaitable.close()
    raise asyncio.TimeoutError


def _body(resp) -> object:
    return json.loads(resp.body)


class _FakeProc:
    """Minimal async subprocess stand-in for model-list branches."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    def kill(self):  # noqa: D401 - matches Process API
        pass

    async def communicate(self):
        return self._stdout, self._stderr


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
        agents.asyncio, "wait_for", new=_raise_timeout
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert "error" in _body(resp)


def test_list_models_nonzero_exit_returns_503():
    proc = _FakeProc(stderr=b"sandbox initialization failed", returncode=71)
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
    ), patch(
        "kiro_crew.platform.redact_via_context", lambda text: text
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list command failed"}


def test_list_models_empty_stdout_returns_503():
    proc = _FakeProc(returncode=0)
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
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned empty output"}


def test_list_models_invalid_json_returns_503():
    proc = _FakeProc(stdout=b"not-json", returncode=0)
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
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned invalid JSON"}


def test_list_models_invalid_payload_returns_503():
    payload = json.dumps({"models": {"unexpected": "mapping"}}).encode()
    proc = _FakeProc(stdout=payload, returncode=0)
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
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned an invalid payload"}


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
    ):
        resp = _run(agents.api_models(_kiro_request()))
    assert resp.status == 200
    models = _body(resp)
    assert any(m["model_name"] == "claude-opus-4.8" for m in models)
