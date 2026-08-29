"""OpenCode ACP adapter contracts.

These tests use protocol fakes and temporary executables only. They never read
the operator's OpenCode configuration, credentials, or session store.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import (
    OPENCODE_BIN_ENV,
    PROTOCOL_VERSION_OPENCODE,
    AcpClient,
    AcpModelUnavailable,
    _resolve_opencode_bin,
)
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE
from kiro_crew.providers.acp import AcpProvider


def _model_options(current: str = "openai/gpt-5") -> list[dict]:
    return [
        {
            "id": "model",
            "name": "Model",
            "currentValue": current,
            "options": [
                {"value": "openai/gpt-5", "name": "GPT-5"},
                {"value": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4"},
            ],
        }
    ]


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class TestOpenCodeResolver:
    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        executable = _make_executable(tmp_path / "custom-opencode")
        monkeypatch.setenv(OPENCODE_BIN_ENV, str(executable))
        monkeypatch.setattr(client_mod, "_mise_which", lambda _tool: "/wrong/mise")

        assert _resolve_opencode_bin() == str(executable.resolve())

    def test_official_install_directory_is_discovered(self, tmp_path, monkeypatch):
        executable = _make_executable(tmp_path / ".opencode" / "bin" / "opencode")
        monkeypatch.delenv(OPENCODE_BIN_ENV, raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(client_mod.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(client_mod.shutil, "which", lambda *_args, **_kwargs: None)

        assert _resolve_opencode_bin() == str(executable.resolve())


class TestOpenCodeProtocol:
    @pytest.mark.asyncio
    async def test_spawn_uses_native_acp_command_and_crew_sandbox(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        wrapped: dict[str, object] = {}

        def _wrap(argv, *, mode, strip_python_env, is_kiro_cli):
            wrapped.update(
                argv=list(argv),
                mode=mode,
                strip_python_env=strip_python_env,
                is_kiro_cli=is_kiro_cli,
            )
            return list(argv), None

        process = MagicMock()
        process.pid = 12345
        process.returncode = None
        process.stdin = None
        process.stdout = None
        process.stderr = None

        with (
            patch(
                "kiro_crew.acp.client._resolve_opencode_bin",
                return_value="/opt/opencode/bin/opencode",
            ),
            patch("kiro_crew.acp.client.wrap_argv", side_effect=_wrap),
            patch("kiro_crew.acp.client.cgroup_scope_argv", side_effect=lambda argv: argv),
            patch(
                "kiro_crew.acp.client.create_subprocess_limited",
                new=AsyncMock(return_value=process),
            ) as spawn,
            patch("kiro_crew.acp.client._get_start_time", return_value=1),
            patch("kiro_crew.acp.client._get_child_pids", return_value=set()),
            patch("kiro_crew.acp.client.inject_xdist_auto_cap"),
            patch("kiro_crew.acp.client.agent_scratch.allocate_scratch", return_value=None),
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            await client._spawn()

        assert wrapped["argv"] == [
            "/opt/opencode/bin/opencode",
            "acp",
            "--cwd",
            str(tmp_path),
        ]
        assert wrapped["is_kiro_cli"] is False
        assert spawn.await_args.kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_new_session_uses_protocol_v1_and_no_kiro_mode(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        sent: list[tuple[str, dict]] = []
        responses = iter(
            [
                {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
                {
                    "sessionId": "oc-session",
                    "configOptions": _model_options(),
                },
            ]
        )

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(_request_id, **_kwargs):
            return next(responses)

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]
        client._drain_notifications = AsyncMock()  # type: ignore[assignment]

        await client._initialize_session()

        assert sent[0][0] == "initialize"
        assert sent[0][1]["protocolVersion"] == PROTOCOL_VERSION_OPENCODE
        assert [method for method, _params in sent] == ["initialize", "session/new"]
        assert client._session_id == "oc-session"
        assert client._resolved_model_id == "openai/gpt-5"

    @pytest.mark.asyncio
    async def test_load_accepts_config_options_without_modes_or_kiro_file(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        client.set_resume_session_id("existing-session")
        sent: list[tuple[str, dict]] = []
        responses = iter(
            [
                {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
                {"configOptions": _model_options()},
            ]
        )

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(_request_id, **_kwargs):
            return next(responses)

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]
        client._drain_notifications = AsyncMock()  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.kiro_sessions_dir") as kiro_store:
            await client._initialize_session()

        assert client.resumed is True
        assert client._session_id == "existing-session"
        assert [method for method, _params in sent] == ["initialize", "session/load"]
        load_params = sent[1][1]
        assert "_meta" not in load_params
        kiro_store.assert_not_called()

    def test_models_are_captured_from_config_options(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)

        client._capture_available_models({"configOptions": _model_options()})

        assert client.available_models() == [
            {"modelId": "openai/gpt-5", "name": "GPT-5", "description": ""},
            {
                "modelId": "anthropic/claude-sonnet-4",
                "name": "Claude Sonnet 4",
                "description": "",
            },
        ]
        assert client._resolved_model_id == "openai/gpt-5"

    @pytest.mark.asyncio
    async def test_model_switch_uses_config_option_and_refreshes_options(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        client._session_id = "oc-session"
        client._available_models = [
            {"modelId": "openai/gpt-5", "name": "GPT-5", "description": ""},
            {
                "modelId": "anthropic/claude-sonnet-4",
                "name": "Claude Sonnet 4",
                "description": "",
            },
        ]
        client._send_request = AsyncMock(return_value=17)  # type: ignore[assignment]
        client._wait_for_response = AsyncMock(  # type: ignore[assignment]
            return_value={"configOptions": _model_options("anthropic/claude-sonnet-4")}
        )

        await client.set_model("anthropic/claude-sonnet-4")

        client._send_request.assert_awaited_once_with(  # type: ignore[union-attr]
            "session/set_config_option",
            {
                "sessionId": "oc-session",
                "configId": "model",
                "value": "anthropic/claude-sonnet-4",
            },
        )
        assert client._resolved_model_id == "anthropic/claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_explicit_unadvertised_model_is_rejected_before_wire(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        client._session_id = "oc-session"
        client._available_models = [{"modelId": "openai/gpt-5", "name": "GPT-5", "description": ""}]
        client._send_request = AsyncMock()  # type: ignore[assignment]

        with pytest.raises(AcpModelUnavailable):
            await client.set_model("provider/missing")

        client._send_request.assert_not_awaited()  # type: ignore[union-attr]


class TestOpenCodeProviderBoundaries:
    def _provider(self, tmp_path) -> AcpProvider:
        provider = AcpProvider(
            work_dir=tmp_path,
            model="openai/gpt-5",
            acp_backend=ACP_BACKEND_OPENCODE,
            effort_per_model={"openai/gpt-5": "high"},
            tool_search=True,
        )
        return provider

    def test_opencode_gets_no_kiro_overlay_or_capabilities(self, tmp_path):
        provider = self._provider(tmp_path)

        provider._apply_effort_overlay()
        provider._apply_tool_search_overlay()

        assert provider.is_opencode_backend is True
        assert provider.is_acp_runtime_backend is False
        assert provider.is_session_sharing_eligible is False
        assert provider.supports_steer is False
        assert provider.supports_effort() is False
        assert not (tmp_path / ".kiro" / "settings" / "cli.json").exists()

    @pytest.mark.asyncio
    async def test_effort_change_fails_closed(self, tmp_path):
        provider = self._provider(tmp_path)
        provider.client.send_command = AsyncMock()  # type: ignore[method-assign]

        assert await provider.change_effort("high") is False
        provider.client.send_command.assert_not_awaited()  # type: ignore[union-attr]
