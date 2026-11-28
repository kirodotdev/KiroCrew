"""Failed project checks hold every name the guarded fallback can read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    folder = tmp_path / "repo"
    path = folder / KIRO_PROJECT_MCP_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"planted": {"command": "example-server"}}}),
        encoding="utf-8",
    )
    spec = {"name": "a", "includeMcpJson": True}
    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda *a, **k: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda directory, *a: (spec, directory))
    return folder


def _boom(*args, **kwargs):
    raise RuntimeError("check unavailable")


def _assert_held(backend, folder):
    from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args

    hold = holds.project_hold(backend, "a", folder)
    assert [server.name for server in hold.pending] == ["planted"]
    assert hold.pending[0].fingerprint == approvals.fingerprint({"command": "example-server"})
    assert hold.unreadable == ()
    assert holds.unreadable_notice(backend, hold) == ""
    [stub] = holds.with_held_servers([], hold.pending, "a", folder)
    assert stub["name"] == "planted"
    [packed] = [arg for arg in stub["args"] if arg.startswith(STUB_FLAGS_FLAG)]
    assert "--refuse" in decode_target_args(packed.split("=", 1)[1])


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("failed_step", ["_load", "_scan"])
def test_failed_folder_check_holds_names(project, monkeypatch, backend, failed_step):
    monkeypatch.setattr(approvals, failed_step, _boom)
    found = approvals.check_folder(project, files=(KIRO_PROJECT_MCP_FILE,))
    assert [server.name for server in found.pending] == ["planted"]
    assert found.unreadable == ()
    _assert_held(backend, project)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_failed_spec_step_holds_names(project, monkeypatch, backend):
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", _boom)
    monkeypatch.setattr(kas, "resolve_projected_spec", _boom)
    _assert_held(backend, project)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_refused_path_gets_a_fresh_screen_before_names_read(project, monkeypatch, backend):
    from kiro_crew.acp.session_mcp_settings_read import (
        _UNREADABLE_REFUSED_PATH,
        SettingsReadRefusal,
    )

    def refuse(path):
        raise SettingsReadRefusal("path check unavailable", _UNREADABLE_REFUSED_PATH)

    monkeypatch.setattr(holds, "read_settings_file", refuse)
    _assert_held(backend, project)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_exhausted_strict_reader_retries_hold_names(project, monkeypatch, backend):
    from kiro_crew import hooks
    from kiro_crew.acp import session_mcp

    validate = hooks.validate_file_path
    attempts = []
    pauses = []

    def saturated(raw):
        attempts.append(raw)
        return None if len(attempts) <= 3 else validate(raw)

    monkeypatch.setattr(hooks, "validate_file_path", saturated)
    monkeypatch.setattr(session_mcp, "_on_loop_thread", lambda: False)
    monkeypatch.setattr(session_mcp, "_sleep", pauses.append)
    _assert_held(backend, project)
    assert len(attempts) == 4
    assert pauses == [pause for pause in session_mcp._SETTINGS_READ_BACKOFF_SECS if pause]


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
def test_path_still_refused_is_never_opened(project, monkeypatch, backend):
    from kiro_crew import hooks, jsonl_util
    from kiro_crew.acp import session_mcp

    def unexpected_open(*args, **kwargs):
        pytest.fail("a refused path must not be opened")

    monkeypatch.setattr(hooks, "validate_file_path", lambda raw: None)
    monkeypatch.setattr(jsonl_util, "open_regular_nofollow", unexpected_open)
    monkeypatch.setattr(session_mcp, "_sleep", lambda delay: None)
    hold = holds.project_hold(backend, "a", project)
    assert hold.pending == []
    assert hold.unreadable == (str(project.resolve() / KIRO_PROJECT_MCP_FILE),)
    assert "Fix or delete that file" in holds.unreadable_notice(backend, hold)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("names_fail", [False, True])
def test_no_readable_names_keeps_notice(project, monkeypatch, backend, names_fail):
    monkeypatch.setattr(approvals, "_load", _boom)
    if names_fail:
        monkeypatch.setattr(holds, "read_server_entries", _boom)
    else:
        (project / KIRO_PROJECT_MCP_FILE).write_text("{", encoding="utf-8")
    hold = holds.project_hold(backend, "a", project)
    assert hold.pending == []
    assert hold.unreadable == (str(project.resolve() / KIRO_PROJECT_MCP_FILE),)
    assert "Fix or delete that file" in holds.unreadable_notice(backend, hold)
