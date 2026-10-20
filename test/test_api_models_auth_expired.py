"""An expired kiro-cli login must make GET /api/models say "run kiro-cli login".

When the kiro-cli auth session has expired or is signed out, ``kiro-cli chat
--list-models`` exits non-zero with auth-failure stderr. The handler classifies
that one exit as a sign-in problem and answers 503 with the
``kiro_prerequisite_required`` code and the "Run ``kiro-cli login``" message, so
the Settings > Chat picker shows the one actionable sign-in message instead of a
generic config-load failure. The readiness gate does not cover this: its latch
is verified at startup and is stale once a login lapses mid-session.

These tests drive the real ``agents.api_models`` request path over a faked
``--list-models`` spawn, asserting:

* an auth-failure exit carries ``code: kiro_prerequisite_required`` and the
  canonical "Run ``kiro-cli login``" message;
* a non-auth exit returns the generic body with no code (fail-safe fence);
* no token text from the stderr reaches the 503 body or any log line (security).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.kiro_readiness import KIRO_PREREQUISITE_REQUIRED_CODE
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# A real auth-failure stderr an expired kiro-cli login writes: a rejected bearer
# token. ``is_auth_failure_output`` recognizes it.
_EXPIRED_AUTH_STDERR = (
    b'GetProfile failed: AccessDeniedException: "Invalid token" (HTTP 400)\n'
    b"Access denied: The bearer token included in the request is invalid.\n"
)
# A non-auth degraded exit: a real backend/service blip that is NOT a sign-in
# problem and must keep the generic 503.
_SERVICE_ERROR_STDERR = b"error: service error (request timed out contacting the backend)\n"
# A fabricated secret planted in the stderr to prove no token text ever reaches
# the user-facing 503 body or the logs.
_FAKE_TOKEN = "aws_session_token=AQoEXAMPLEFAKEx8sS1TOKENxSHOULDNEVERLEAK1234567890"


class _Proc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 99_999_999_999  # above every pid_max: no kill reaches a real process

    def kill(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _Spawner:
    def __init__(self, proc: _Proc) -> None:
        self.proc = proc

    async def __call__(self, *a: Any, **k: Any) -> _Proc:
        del a, k
        return self.proc


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
    request.app = {"kiro_prerequisite_service": service}
    return request


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """A fresh catalog cache and the spawn/sandbox wrappers faked out."""
    monkeypatch.setattr(agents, "_catalog_cache", agents._CatalogCache())
    cfg = SimpleNamespace(agent=SimpleNamespace(provider="kiro", acp_backend="kiro"))
    patches = [
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.acp.client._resolve_kiro_bin_for_spawn", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client._resolve_ssh_auth_sock", lambda env: None),
        patch("kiro_crew.env.augmented_path", lambda p: p),
        patch.object(agents, "wrap_argv", lambda argv, **kwargs: (argv, None)),
        patch.object(agents, "cgroup_scope_argv", lambda argv: argv),
        patch("kiro_crew.sandbox.resource_limit_preexec", lambda: None),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


def _poll(tmp_path: Path, proc: _Proc):
    with patch.object(agents, "spawn_supervised_oneshot", _Spawner(proc)):
        return asyncio.run(agents.api_models(_request(tmp_path)))


def test_expired_auth_exit_returns_the_sign_in_code_and_message(harness, tmp_path):
    """An auth-failure exit carries the sign-in code and the actionable message."""
    resp = _poll(tmp_path, _Proc(b"", _EXPIRED_AUTH_STDERR, 1))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["code"] == KIRO_PREREQUISITE_REQUIRED_CODE
    assert "kiro-cli login" in body["error"]
    # Not the generic message, and no retry/switch-model misdirection.
    assert body["error"] != "model list command failed"
    assert "5xx" not in body["error"]


def test_non_auth_exit_still_returns_the_generic_body(harness, tmp_path):
    """A genuine service blip stays generic: the generic code, not the sign-in one."""
    resp = _poll(tmp_path, _Proc(b"", _SERVICE_ERROR_STDERR, 1))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body == {"error": "model list command failed", "code": "model_list_unavailable"}
    assert body["code"] != KIRO_PREREQUISITE_REQUIRED_CODE


def test_no_token_text_reaches_the_body_or_the_log(harness, tmp_path, caplog):
    """An auth exit whose stderr also carries a secret must leak none of it."""
    caplog.set_level(logging.DEBUG)
    stderr = _EXPIRED_AUTH_STDERR + f"debug: {_FAKE_TOKEN}\n".encode()
    resp = _poll(tmp_path, _Proc(b"", stderr, 1))
    assert resp.status == 503
    body = json.loads(resp.body)
    # The user-facing body is the fixed sign-in message - never the raw stderr.
    assert _FAKE_TOKEN not in resp.body.decode()
    assert "aws_session_token" not in body["error"]
    # And nothing logged this turn echoes the token verbatim.
    for record in caplog.records:
        assert _FAKE_TOKEN not in record.getMessage()
