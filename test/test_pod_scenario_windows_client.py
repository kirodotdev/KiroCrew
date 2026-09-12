from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from e2e.scenarios import conftest as scenarios_conftest

from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import PodConfig


class _Response:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Opener:
    def __init__(self) -> None:
        self.handlers: list[object] = []
        self.requests: list[urllib.request.Request] = []

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)

    def open(self, request: urllib.request.Request, *, timeout: float):
        assert timeout > 0
        self.requests.append(request)
        if len(self.requests) == 1:
            return _Response()
        return _Response(b'{"ok": true}')


def _client(tmp_path: Path) -> scenarios_conftest.PodClient:
    plane = tmp_path / "plane"
    plane.mkdir()
    return scenarios_conftest.PodClient(
        name="worktree",
        base_url="http://127.0.0.1:7411",
        port=7411,
        home=plane / "h" / "worktree",
        checkout=tmp_path,
        cli=tmp_path / ".venv" / "Scripts" / "kirocrew.exe",
        env={"KIROCREW_POD_ROOT": str(plane / "h")},
    )


def test_windows_client_exchanges_token_once_then_uses_cookie_urls(tmp_path, monkeypatch):
    opener = _Opener()
    minted: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        minted.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="one-time/secret+value\n", stderr="")

    monkeypatch.setattr(scenarios_conftest.subprocess, "run", fake_run)
    monkeypatch.setattr(scenarios_conftest, "build_loopback_opener", lambda: opener)
    client = _client(tmp_path)

    assert client._api_windows("GET", "crons", None, expect_ok=True, timeout=5) == {"ok": True}
    assert client._api_windows("GET", "chat/slots", None, expect_ok=True, timeout=5) == {"ok": True}

    assert minted == [[str(client.cli), "pod", "token", client.name]]
    assert len(opener.handlers) == 1
    assert isinstance(opener.handlers[0], urllib.request.HTTPCookieProcessor)
    urls = [request.full_url for request in opener.requests]
    assert urls[0].startswith("http://127.0.0.1:7411/api/status?token=")
    assert "one-time/secret+value" not in urls[0]  # reserved characters are encoded
    assert urls[1:] == [
        "http://127.0.0.1:7411/api/crons",
        "http://127.0.0.1:7411/api/chat/slots",
    ]
    assert all("token=" not in url for url in urls[1:])


def test_scenario_backend_uses_supported_launcher(tmp_path, monkeypatch):
    from kiro_crew.testing import harness

    launcher = tmp_path / "kiro-backend.cmd"
    launcher.write_text("shim", encoding="utf-8")
    calls: list[Path] = []

    def fake_launcher(directory: Path) -> Path:
        calls.append(directory)
        return launcher

    monkeypatch.setattr(harness, "fake_acp_backend_launcher", fake_launcher)
    monkeypatch.delenv("KIROCREW_E2E_SCENARIOS_REAL_AGENT", raising=False)

    assert scenarios_conftest._resolve_backend(tmp_path) == launcher
    assert calls == [tmp_path]


def test_windows_token_exchange_error_does_not_render_credential(tmp_path, monkeypatch):
    class RefusingOpener(_Opener):
        def open(self, request: urllib.request.Request, *, timeout: float):
            raise urllib.error.URLError(f"refused {request.full_url}")

    monkeypatch.setattr(
        scenarios_conftest.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="do-not-render-me\n", stderr=""
        ),
    )
    monkeypatch.setattr(scenarios_conftest, "build_loopback_opener", RefusingOpener)

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _client(tmp_path)._api_windows("GET", "crons", None, expect_ok=True, timeout=5)

    assert "do-not-render-me" not in str(exc_info.value)
    assert "token=" not in str(exc_info.value)


def _plane_config(scratch: Path) -> tuple[PodConfig, dict[str, str]]:
    cfg = PodConfig(
        pod_root=scratch / "h",
        pods_dir=scratch / "e",
        artifacts_dir=scratch / "a",
        base_port=7410,
        live_port=5476,
        unit_prefix=scenarios_conftest.PLANE_PREFIX,
        gateway_path="isolated-path",
        repo_hint=None,
        worktrees_root=None,
    )
    env = {
        "KIROCREW_POD_ROOT": str(cfg.pod_root),
        "KIROCREW_POD_ENV_DIR": str(cfg.pods_dir),
        "KIROCREW_POD_ARTIFACTS_DIR": str(cfg.artifacts_dir),
        "KIROCREW_POD_BASE_PORT": str(cfg.base_port),
        "KIROCREW_POD_LIVE_PORT": str(cfg.live_port),
        "KIROCREW_POD_UNIT_PREFIX": cfg.unit_prefix,
        "PATH": cfg.gateway_path,
    }
    return cfg, env


def test_force_cleanup_keeps_windows_task_and_plane_without_writer_proof(tmp_path, monkeypatch):
    scratch = tmp_path / "plane"
    cfg, env = _plane_config(scratch)
    script = win_backend.task_script_path(cfg, "worktree")
    script.parent.mkdir(parents=True)
    script.write_text("keep", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(scenarios_conftest.sys, "platform", "win32")
    monkeypatch.setattr(win_backend, "schtasks", lambda *args: calls.append(args))
    monkeypatch.setattr(scenarios_conftest, "_kill_plane_processes", lambda _scratch: [])
    monkeypatch.setattr(scenarios_conftest, "_matching_plane_processes", lambda _scratch: None)

    services, killed, removed = scenarios_conftest._force_clean_plane(scratch, "worktree", env)

    assert services == [win_backend.task_name(cfg, "worktree")]
    assert killed == []
    assert removed is False
    assert calls == [("/End", "/TN", win_backend.task_name(cfg, "worktree"))]
    assert script.read_text(encoding="utf-8") == "keep"
    assert scratch.exists()


def test_force_cleanup_deletes_only_exact_windows_task_after_writer_proof(tmp_path, monkeypatch):
    scratch = tmp_path / "plane"
    cfg, env = _plane_config(scratch)
    script = win_backend.task_script_path(cfg, "worktree")
    script.parent.mkdir(parents=True)
    script.write_text("wrapper", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_schtasks(*args):
        calls.append(args)
        rc = 1 if args[0] == "/Query" else 0
        return subprocess.CompletedProcess(args, rc)

    monkeypatch.setattr(scenarios_conftest.sys, "platform", "win32")
    monkeypatch.setattr(win_backend, "schtasks", fake_schtasks)
    monkeypatch.setattr(scenarios_conftest, "_kill_plane_processes", lambda _scratch: [])
    monkeypatch.setattr(scenarios_conftest, "_matching_plane_processes", lambda _scratch: [])

    services, killed, removed = scenarios_conftest._force_clean_plane(scratch, "worktree", env)

    exact = win_backend.task_name(cfg, "worktree")
    assert services == [exact]
    assert killed == []
    assert removed is True
    assert calls == [
        ("/End", "/TN", exact),
        ("/Delete", "/TN", exact, "/F"),
        ("/Query", "/TN", exact),
    ]
    assert not scratch.exists()
