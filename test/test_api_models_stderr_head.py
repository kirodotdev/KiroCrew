"""A failed --list-models logs the redacted start of a stderr its tail cut.

The WARNING for a non-zero ``kiro-cli chat --list-models`` exit keeps only the
last ``_MODEL_LIST_STDERR_TAIL_CHARS`` of stderr, so on a long stderr the first
error line was lost behind the warnings printed after it. The handler now logs
the start of that stderr too, only when the tail cut something, through the same
redaction, and the 503 body is unchanged.
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
from redaction_structured_cases import session_token

from kiro_crew.dashboard.handlers import agents
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# A synthetic session token from the same helper and seed as
# test_api_models_auth_expired.py, cut to 200 characters so it fits in the head.
_FAKE_TOKEN = "aws_session_token=" + session_token(length=200, seed=111)
_FIRST_ERROR = "error: first failure line"
_FILLER = "Warning: a later line the tail keeps\n"
_HEAD_PREFIX = "api_models: --list-models stderr began"


class _Proc:
    def __init__(self, stderr: bytes) -> None:
        self._stderr = stderr
        self.returncode = 1
        self.pid = 99_999_999_999  # above every pid_max: no kill reaches a real process

    def kill(self) -> None:
        pass

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


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


def _poll(tmp_path: Path, stderr: str):
    with patch.object(agents, "spawn_supervised_oneshot", _Spawner(_Proc(stderr.encode()))):
        return asyncio.run(agents.api_models(_request(tmp_path)))


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == agents.logger.name]


def _head_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for m in _messages(caplog) if m.startswith(_HEAD_PREFIX)]


def test_long_stderr_logs_its_start_redacted(harness, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    stderr = f"{_FIRST_ERROR} {_FAKE_TOKEN}\n" + _FILLER * 60
    assert len(stderr) > agents._MODEL_LIST_STDERR_TAIL_CHARS + 300
    resp = _poll(tmp_path, stderr)
    heads = _head_lines(caplog)
    assert len(heads) == 1
    head = heads[0]
    assert _FIRST_ERROR in head
    assert "first 300 of" in head
    for message in _messages(caplog):
        assert _FAKE_TOKEN not in message
    assert json.loads(resp.body) == {
        "error": "model list command failed",
        "code": "model_list_unavailable",
    }


def test_short_stderr_logs_no_head(harness, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    stderr = f"{_FIRST_ERROR}\n" + _FILLER * 10
    assert len(stderr.strip()) <= agents._MODEL_LIST_STDERR_TAIL_CHARS
    resp = _poll(tmp_path, stderr)
    assert _head_lines(caplog) == []
    assert any(_FIRST_ERROR in m for m in _messages(caplog))
    assert resp.status == 503
    assert json.loads(resp.body) == {
        "error": "model list command failed",
        "code": "model_list_unavailable",
    }


def test_head_logs_only_what_the_tail_cut(harness, tmp_path, caplog):
    """Between the tail cap and cap + head, the head stops where the tail begins."""
    caplog.set_level(logging.WARNING)
    cut = 40
    stderr = "x" * cut + "y" * agents._MODEL_LIST_STDERR_TAIL_CHARS
    _poll(tmp_path, stderr)
    heads = _head_lines(caplog)
    assert len(heads) == 1
    head = heads[0]
    assert head.endswith(": " + "x" * cut)
    assert f"first {cut} of {cut} chars" in head
