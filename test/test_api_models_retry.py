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
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew import sandbox
from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _stub_wrap_argv(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
    """Pass-through stand-in for ``sandbox.wrap_argv``.

    Absorbs the keyword arguments the real signature takes, so a call site adding
    one — e.g. the explicit ``mode=configured_sandbox_mode()`` that keeps this
    endpoint on the same tier as chat — cannot become a ``TypeError`` here and be
    reported as the degraded 503 these tests assert for other reasons.
    """
    del kwargs
    return argv, None


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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 200
    models = _body(resp)
    assert any(m["model_name"] == "claude-opus-4.8" for m in models)


def test_successful_list_launches_resolved_binary_in_place(tmp_path, monkeypatch):
    # The resolved binary is exec'd at its own path with no inherited snapshot
    # descriptor: a copy/memfd would strand a multi-call CLI's sibling
    # subcommand executable and every spawn would fail with ENOENT.
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    resolved = "/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli"
    spawn = AsyncMock(return_value=_FakeProc(payload))
    env_file = tmp_path / ".env"
    env_file.write_text("KIRO_API_KEY=model-list-key\n", encoding="utf-8")
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.setattr("kiro_crew.config.loader.env_path", lambda: env_file)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE-secret")
    monkeypatch.setenv("PYTHONPATH", "/gateway/pythonpath")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
    monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value=resolved),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch("kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv),
        patch("kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv),
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
    env = spawn.await_args.kwargs["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "PYTHONPATH" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert env["KIRO_API_KEY"] == "model-list-key"
    assert env["AWS_ACCESS_KEY_ID"] == "FAKE-akid"
    assert env["KIROCREW_UNRELATED_KEEPME"] == "keep-this-value"


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
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))
    assert resp.status == 200
    # The non-registry GPT window is now resolvable through the central authority.
    assert mr.model_window("gpt-5.6-terra") == 272000


# ---------------------------------------------------------------------------
# The CONFIGURED sandbox tier (agent.sandbox), not wrap_argv's parameter default
# ---------------------------------------------------------------------------


def test_list_models_spawns_at_the_configured_sandbox_tier(tmp_path):
    """The spawn asks for ``agent.sandbox``, never wrap_argv's ``"auto"`` default.

    ``wrap_argv``'s mode parameter defaults to ``"auto"``, which ignores what the
    operator configured. Where ``agent.sandbox`` is an explicit ``"off"``
    (isolation deferred to kiro-cli's own internal sandbox, which cannot nest
    inside Kiro Crew's), taking that default asks for a STRICTER tier than chat
    itself runs under. Explicit Windows Kiro classification delegates either
    tier, but the configured-tier invariant remains cross-platform.
    """
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    seen: dict[str, Any] = {}

    def _record(argv, **kwargs):
        seen.update(kwargs)
        return argv, None

    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.dashboard.handlers.agents.configured_sandbox_mode", lambda: "off"
    ), patch(
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _record
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ):
        resp = _run(agents.api_models(_kiro_request(tmp_path)))

    assert resp.status == 200
    # Explicitly passed, and passed the CONFIGURED value — not defaulted.
    assert seen.get("mode") == "off", seen


def test_configured_sandbox_mode_is_the_tier_the_chat_path_uses():
    """The helper returns ``agent.sandbox`` verbatim.

    This is the whole invariant: a one-shot ``kiro-cli`` read must resolve to the
    SAME tier the interactive ACP spawn threads through its ``sandbox_mode``
    constructor argument, so the two can never diverge into "chat works but the
    model list 503s".
    """
    for configured in ("off", "auto"):
        cfg = SimpleNamespace(agent=SimpleNamespace(sandbox=configured))
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            assert sandbox.configured_sandbox_mode() == configured


def test_configured_sandbox_mode_fails_secure_when_config_unreadable(caplog):
    """An unreadable config must not become a way to obtain a LOOSER tier."""
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", side_effect=OSError("boom")):
        with caplog.at_level(logging.WARNING, logger=sandbox.logger.name):
            assert sandbox.configured_sandbox_mode() == sandbox._SANDBOX_MODE_FALLBACK
    # Never silently: the substituted tier is announced.
    assert any(r.levelno == logging.WARNING for r in caplog.records), caplog.text


def test_sandbox_refusal_is_reported_with_a_machine_readable_code(tmp_path, caplog):
    """A genuine sandbox refusal is a 503 that NAMES itself.

    Reached only when the operator has actually configured ``agent.sandbox="auto"``
    on a backendless host. Retrying cannot clear it, so it must not be
    indistinguishable from the timeout branch: the body carries a ``code`` the UI
    can branch on, and the log carries the sandbox layer's own remedy text.
    """

    def _refuse(argv, **kwargs):
        raise sandbox.SandboxUnavailableError(
            "Sandbox backend unavailable and allow_unsandboxed_exec is not set.",
            kind="no_backend",
            detail="not Linux",
        )

    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.env.augmented_path", lambda p: p), patch(
        "kiro_crew.dashboard.handlers.agents.configured_sandbox_mode", lambda: "auto"
    ), patch(
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _refuse
    ):
        with caplog.at_level(logging.WARNING, logger=agents.logger.name):
            resp = _run(agents.api_models(_kiro_request(tmp_path)))

    # 503, not 4xx: the "degraded, keep the last-good list" client contract is
    # what stops the picker caching an empty result.
    assert resp.status == 503
    assert _body(resp) == {
        "error": "model list unavailable",
        "code": "model_list_sandbox_unavailable",
    }
    # The remedy reaches the operator rather than a bare traceback.
    assert any("not Linux" in r.getMessage() for r in caplog.records), caplog.text


def test_ssh_auth_sock_resolver_runs_off_the_event_loop(tmp_path):
    """``_resolve_ssh_auth_sock`` globs ``/tmp/ssh-*/agent.*`` and ``os.stat``s
    every hit, so its latency scales with the ``/tmp`` entry count. The frontend
    polls this endpoint every 8s while the model list is degraded, so an on-loop
    call stalls chat, cron and the liveness heartbeat on exactly the host where
    the probe is slowest. Its sibling wrapper's docstring states the rule: never
    on the event loop. Same shape as
    ``test_acp_spawn_offload.py``'s resolver assertions.
    """
    payload = json.dumps({"models": [{"model_name": "claude-opus-4.8"}]}).encode()
    seen: list[threading.Thread] = []

    def _probe(env):
        seen.append(threading.current_thread())
        return None

    async def _drive():
        loop_thread = threading.current_thread()
        resp = await agents.api_models(_kiro_request(tmp_path))
        return loop_thread, resp

    with patch.object(agents.KiroCrewConfig, "load", return_value=_kiro_cfg()), patch(
        "kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"
    ), patch("kiro_crew.acp.client._resolve_ssh_auth_sock", _probe), patch(
        "kiro_crew.env.augmented_path", lambda p: p
    ), patch(
        "kiro_crew.dashboard.handlers.agents.wrap_argv", _stub_wrap_argv
    ), patch(
        "kiro_crew.dashboard.handlers.agents.cgroup_scope_argv", lambda argv: argv
    ), patch(
        "kiro_crew.sandbox.resource_limit_preexec", lambda: None
    ), patch.object(
        agents.asyncio, "create_subprocess_exec", return_value=_FakeProc(payload)
    ):
        loop_thread, resp = _run(_drive())

    assert resp.status == 200
    assert seen, "_resolve_ssh_auth_sock was never called by api_models"
    for thread in seen:
        assert thread is not loop_thread, "ssh resolver ran on the event loop thread"
