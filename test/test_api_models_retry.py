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
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew import sandbox
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# sandbox.agent_sandbox_mode imports KiroCrewConfig lazily INSIDE the function
# (sandbox is a low-level dep of the config loader), so there is no
# ``sandbox.KiroCrewConfig`` attribute to patch — patch the class attribute on
# the loader itself, which the lazy import then resolves to.
_CONFIG_LOAD = "kiro_crew.config.loader.KiroCrewConfig.load"


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _kiro_request(tmp_path: Path) -> MagicMock:
    # api_models is readiness-gated (a signed-out gateway must not spawn a
    # browser-opening kiro-cli), so every degraded-branch test has to get past
    # the fail-closed gate first. `assume_ready=True` is the documented test
    # bypass (see kiro_readiness.reject_if_kiro_unverified); without it these
    # tests would assert the gate's 503 instead of the branch under test.
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        audit_writer=_no_audit,
        assume_ready=True,
    )
    request = MagicMock()
    request.app = {"kiro_prerequisite_service": service}
    return request


def _kiro_cfg(sandbox: str | None = None) -> SimpleNamespace:
    # Any non-"claude_code" provider takes the subprocess path under test.
    # ``sandbox`` mirrors config.json's ``agent.sandbox``; omitted (None) means
    # the key is ABSENT, which agent_sandbox_mode() resolves to the shipped
    # default "off" (isolation delegated to kiro-cli's internal sandbox).
    agent: SimpleNamespace = SimpleNamespace(provider="kiro")
    if sandbox is not None:
        agent.sandbox = sandbox
    return SimpleNamespace(agent=agent)


def _wrap_argv_stub(argv, mode=None, **kwargs):
    """Stand-in for sandbox.wrap_argv that records nothing and never wraps.

    Accepts ``mode`` because api_models now passes the CONFIGURED sandbox tier
    (``agent_sandbox_mode()``) instead of relying on wrap_argv's "auto" default.
    """
    del mode, kwargs
    return argv, None


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


def test_kiro_binary_unresolved_returns_503(tmp_path):
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value=""
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert "error" in _body(resp)


def test_list_models_timeout_returns_503(tmp_path):
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc()
    ), patch.object(
        agents.asyncio, "wait_for", new=_raise_timeout
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert "error" in _body(resp)


def test_list_models_nonzero_exit_returns_503(tmp_path):
    proc = _FakeProc(stderr=b"sandbox initialization failed", returncode=71)
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch(
        "kiro_crew.platform.redact_via_context", lambda text: text
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list command failed"}


def test_list_models_empty_stdout_returns_503(tmp_path):
    proc = _FakeProc(returncode=0)
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned empty output"}


def test_list_models_invalid_json_returns_503(tmp_path):
    proc = _FakeProc(stdout=b"not-json", returncode=0)
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned invalid JSON"}


def test_list_models_invalid_payload_returns_503(tmp_path):
    payload = json.dumps({"models": {"unexpected": "mapping"}}).encode()
    proc = _FakeProc(stdout=payload, returncode=0)
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=proc
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503
    assert _body(resp) == {"error": "model list returned an invalid payload"}


def test_unexpected_exception_returns_503(tmp_path):
    # A failure inside the try (here: kiro-bin resolution raising) must be
    # caught and surfaced as 503, not a cached empty 200.
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", side_effect=RuntimeError("boom")
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 503


def test_successful_list_returns_200_with_models(tmp_path):
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8", "description": "x"}]}).encode()
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 200
    models = _body(resp)
    assert any(m["model_name"] == "claude-opus-4.8" for m in models)


def test_successful_list_launches_resolved_binary_in_place(tmp_path):
    # The resolved binary is exec'd at its own path with no inherited snapshot
    # descriptor: a copy/memfd would strand a multi-call CLI's sibling
    # subcommand executable and every spawn would fail with ENOENT.
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    resolved = "/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli"
    spawn = AsyncMock(return_value=_FakeProc(payload))
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value=resolved),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch("kiro_crew.sandbox.wrap_argv", _wrap_argv_stub),
        patch("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
        patch.object(agents.asyncio, "create_subprocess_exec", spawn),
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))

    assert resp.status == 200
    # Position, not argv[0]: a sandbox/cgroup wrapper may precede the binary.
    argv = list(spawn.await_args.args)
    assert resolved in argv, argv
    assert not any("kiro-cli-snapshots" in str(a) for a in argv), argv
    assert "pass_fds" not in spawn.await_args.kwargs


def test_structured_context_window_seeds_central_authority(tmp_path):
    # kiro-cli's --list-models --format json returns a STRUCTURED
    # context_window_tokens per model. api_models seeds the central window
    # authority (refresh_kiro_windows) from it, so the ACP backfill / context
    # budget scaler can resolve a non-registry model's REAL window (GPT 272k)
    # instead of a guessed default. (This fork keeps kiro's bare-dotted ids as
    # the picker wire format, so the response rows are NOT canonicalized — only
    # the window cache is seeded; see api_models.)
    import kiro_crew.model_registry as mr

    payload = json.dumps(
        {
            "models": [
                {
                    "model_name": "gpt-5.6-terra",
                    "model_id": "gpt-5.6-terra",
                    "description": "Experimental preview of OpenAI GPT 5.6 Terra with 272k context window",
                    "context_window_tokens": 272000,
                },
                {
                    "model_name": "claude-opus-4.8",
                    "model_id": "claude-opus-4.8",
                    "description": "Claude Opus 4.8 model with 1M context window",
                    "context_window_tokens": 1000000,
                },
            ]
        }
    ).encode()
    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.sandbox.wrap_argv", _wrap_argv_stub
    ), patch(
        "kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ), patch.object(
        agents.asyncio, "wait_for", return_value=(payload, b"")
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 200
    # The non-registry GPT window is now resolvable through the central authority.
    assert mr.model_window("gpt-5.6-terra") == 272000


class TestSandboxTierFollowsConfig:
    """The one-shot ``--list-models`` spawn must run at the CONFIGURED tier.

    Regression: the handler called ``wrap_argv(argv)`` and inherited wrap_argv's
    hardcoded ``mode="auto"`` default, which resolves to ``standard`` and demands
    an OS-level sandbox backend. On any host with no backend — Windows always,
    Linux without user namespaces — wrap_argv fail-closes with a RuntimeError,
    the handler's broad ``except`` turned it into a 503, and the frontend ACP
    adapter degraded to its auto-only fallback list. The visible symptom was a
    model picker offering nothing but "Auto", with no way to pick another model.

    ``AcpClient._spawn`` runs the SAME binary at ``mode=cfg.agent.sandbox``
    (default ``"off"``), so confining the read-only listing more tightly than
    the interactive agent session it describes bought nothing.
    """

    def _patches(self, cfg, payload, wrap_argv):
        return (
            patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
            patch(
                "kiro_crew.acp.client._resolve_kiro_bin_for_spawn",
                return_value="/usr/bin/kiro-cli",
            ),
            patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
            patch("kiro_crew.env.augmented_path", lambda p: p),
            patch("kiro_crew.sandbox.wrap_argv", wrap_argv),
            patch("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv),
            patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
            patch.object(
                agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
            ),
        )

    def _run_with(self, tmp_path, cfg, wrap_argv):
        payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
        with contextlib.ExitStack() as stack:
            for p in self._patches(cfg, payload, wrap_argv):
                stack.enter_context(p)
            return _run(agents.api_models(_kiro_request(tmp_path)))

    def test_absent_sandbox_key_requests_off(self, tmp_path):
        # agent.sandbox unset -> the shipped default, NOT wrap_argv's "auto".
        seen: list[str | None] = []

        def _wrap(argv, mode=None, **kwargs):
            del kwargs
            seen.append(mode)
            return argv, None

        resp = self._run_with(tmp_path, _kiro_cfg(), _wrap)
        assert resp.status == 200
        assert seen == ["off"]

    def test_operator_sandbox_auto_is_honored(self, tmp_path):
        # An operator who explicitly opts back into KiroCrew's OS sandbox still
        # gets it — this fix follows config, it does not pin the tier to "off".
        seen: list[str | None] = []

        def _wrap(argv, mode=None, **kwargs):
            del kwargs
            seen.append(mode)
            return argv, None

        resp = self._run_with(tmp_path, _kiro_cfg(sandbox="auto"), _wrap)
        assert resp.status == 200
        assert seen == ["auto"]

    def test_no_sandbox_backend_still_lists_models(self, tmp_path):
        # Simulate a Windows / no-userns host: wrap_argv fail-closes for any
        # tier that needs a backend, but passes "off" through untouched. The
        # picker must get a real 200 list, not the degraded 503.
        def _wrap(argv, mode=None, **kwargs):
            del kwargs
            if mode != "off":
                raise RuntimeError(
                    "Sandbox backend unavailable and allow_unsandboxed_exec is not set."
                )
            return argv, None

        resp = self._run_with(tmp_path, _kiro_cfg(), _wrap)
        assert resp.status == 200
        assert any(m["model_name"] == "claude-opus-4.8" for m in _body(resp))


class TestAgentSandboxMode:
    """``sandbox.agent_sandbox_mode`` resolution, including the fail-secure path."""

    def test_absent_key_is_off(self):
        with patch(_CONFIG_LOAD, return_value=_kiro_cfg()):
            assert sandbox.agent_sandbox_mode() == "off"

    def test_explicit_values_pass_through(self):
        for mode in ("auto", "standard", "strict", "cc", "off"):
            with patch(_CONFIG_LOAD, return_value=_kiro_cfg(sandbox=mode)):
                assert sandbox.agent_sandbox_mode() == mode

    def test_malformed_value_fails_secure_to_auto(self):
        # A typo/wrong type is a misconfiguration, never an intent to run
        # unconfined — so it must NOT resolve to "off".
        for bad in ("standrd", "on", 1, True, [], object()):
            with patch(_CONFIG_LOAD, return_value=_kiro_cfg(sandbox=bad)):
                assert sandbox.agent_sandbox_mode() == "auto"

    def test_config_load_failure_fails_secure_to_auto(self):
        with patch(_CONFIG_LOAD, side_effect=RuntimeError("boom")):
            assert sandbox.agent_sandbox_mode() == "auto"
