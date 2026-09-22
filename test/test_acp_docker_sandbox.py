"""Docker-container confinement for the OpenCode harness.

Pure-builder pins plus predicate checks. No Docker daemon is ever touched:
every probe seam is monkeypatched, home directories are ``tmp_path``, and no
test reads the operator's real config -- the flag is pinned through the module
function the predicate actually calls.
"""

from __future__ import annotations

import inspect

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk import docker_sandbox as docker_sb


def _scrubbed_env() -> dict[str, str]:
    """A post-scrub session env: values are clean, pointers are the test."""
    return {
        "OPENCODE_CONFIG_CONTENT": '{"permission": "ask"}',
        "CUSTOM_MODEL_KEY": "sk-session-123",
        "HOME": "C:\\Users\\jorda",
        "USERPROFILE": "C:\\Users\\jorda",
        "PATH": "C:\\Windows\\System32",
        "TEMP": "C:\\Temp",
        "TMPDIR": "C:\\Temp",
        "XDG_CONFIG_HOME": "C:\\Users\\jorda\\.config",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "KIRO_CHAT_LOG_FILE": "C:\\Users\\jorda\\scratch\\kiro-chat.log",
        "KIROCREW_RUNTIME_PYTHON": "C:\\Python\\python.exe",
        "KIROCREW_SPAWNED": "1",
    }


def test_container_env_drops_host_pointers_and_pins_its_own() -> None:
    translated = docker_sb.container_env_from(_scrubbed_env())
    # Session content survives: the seed and the operator's session key.
    assert translated["OPENCODE_CONFIG_CONTENT"] == '{"permission": "ask"}'
    assert translated["CUSTOM_MODEL_KEY"] == "sk-session-123"
    # Host pointers do not cross, in either case. HOME and PATH are re-pinned
    # below rather than dropped, so they are excluded from this absence check,
    # and so are the temp/scratch pointers the next test covers.
    for dropped in (
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "SSH_AUTH_SOCK",
        "KIRO_CHAT_LOG_FILE",
        "KIROCREW_RUNTIME_PYTHON",
        "KIROCREW_SPAWNED",
    ):
        assert dropped not in translated, dropped
    # The container's own identity is pinned, not inherited.
    assert translated["HOME"] == "/root"
    assert translated["PATH"] != _scrubbed_env()["PATH"]
    assert "usr/local/bin" in translated["PATH"]


def test_container_env_re_pins_temp_and_scratch_inside() -> None:
    """A host ``C:\\...`` temp has no container spelling, so temp and scratch
    are re-pinned to container paths rather than arriving dangling."""
    translated = docker_sb.container_env_from(_scrubbed_env())
    assert translated["TMPDIR"] == "/tmp"
    assert translated["TMP"] == "/tmp"
    assert translated["TEMP"] == "/tmp"
    assert translated["KIROCREW_SCRATCH"] == "/tmp/kirocrew-scratch"


def test_session_argv_mounts_only_workspace_and_auth(tmp_path) -> None:
    work = tmp_path / "work space"
    work.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    argv = docker_sb.session_argv(
        work_dir=str(work),
        scrubbed_env={"OPENCODE_CONFIG_CONTENT": "{}"},
        auth_file=str(auth),
    )
    assert argv[:6] == ["docker", "run", "--rm", "-i", "--pull", "never"]
    assert "--init" in argv
    assert argv[argv.index("-w") + 1] == "/workspace"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 2, mounts
    assert mounts[0].endswith(":/workspace") and ":ro" not in mounts[0]
    assert mounts[1].endswith(":/root/.local/share/opencode/auth.json:ro")
    # Confinement claim: no credential home is mounted, and a missing image
    # fails instead of fetching.
    assert not any(".aws" in m or ".ssh" in m for m in mounts)
    # Hardening: no new privileges, no capabilities, clean tmpfs temp.
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    assert docker_sb.IMAGE_REF in argv
    assert argv[-2:] == ["opencode", "acp"]


def test_session_argv_mounts_state_config_then_auth_in_order(tmp_path) -> None:
    """State (rw) then config (ro) then auth (ro): the auth bind lands last so
    it overlays the read-write state dir at the same tree."""
    work = tmp_path / "work"
    work.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    argv = docker_sb.session_argv(
        work_dir=str(work),
        scrubbed_env={},
        auth_file=str(auth),
        state_dir=str(state),
        config_dir=str(config),
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 4, mounts
    assert mounts[0].endswith(":/workspace")
    assert mounts[1].endswith(":/root/.local/share/opencode") and ":ro" not in mounts[1]
    assert mounts[2].endswith(":/root/.config/opencode:ro")
    assert mounts[3].endswith(":/root/.local/share/opencode/auth.json:ro")
    # The auth mount string comes after the state mount string: the overlap
    # order the confinement claim rests on.
    joined = " ".join(argv)
    assert joined.index(":/root/.local/share/opencode ") < joined.index("auth.json:ro")


def test_session_argv_without_auth_mounts_workspace_only(tmp_path) -> None:
    argv = docker_sb.session_argv(
        work_dir=str(tmp_path),
        scrubbed_env={},
        auth_file=None,
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 1 and mounts[0].endswith(":/workspace")


def test_predicate_is_opencode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_sb, "sandbox_docker_enabled", lambda: True)
    monkeypatch.setattr(docker_sb, "daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(docker_sb, "daemon_ostype", lambda *a, **k: "linux")
    monkeypatch.setattr(docker_sb, "image_available", lambda *a, **k: True)
    assert docker_sb.docker_sandbox_applies(ACP_BACKEND_OPENCODE) is True
    assert docker_sb.docker_sandbox_applies(ACP_BACKEND_CODEX) is False


def test_check_names_each_missing_piece_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_sb, "sandbox_docker_enabled", lambda: False)
    reason = docker_sb.check_docker_sandbox()
    assert reason is not None and "sandbox_docker" in reason

    monkeypatch.setattr(docker_sb, "sandbox_docker_enabled", lambda: True)
    monkeypatch.setattr(docker_sb, "daemon_reachable", lambda *a, **k: False)
    reason = docker_sb.check_docker_sandbox()
    assert reason is not None and "daemon" in reason

    monkeypatch.setattr(docker_sb, "daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(docker_sb, "daemon_ostype", lambda *a, **k: "windows")
    reason = docker_sb.check_docker_sandbox()
    assert reason is not None and "Linux containers" in reason

    monkeypatch.setattr(docker_sb, "daemon_ostype", lambda *a, **k: "linux")
    monkeypatch.setattr(docker_sb, "image_available", lambda *a, **k: False)
    reason = docker_sb.check_docker_sandbox()
    assert reason is not None and "docker build" in reason

    monkeypatch.setattr(docker_sb, "image_available", lambda *a, **k: True)
    assert docker_sb.check_docker_sandbox() is None


def test_probe_cache_bypassed_on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn verdict re-probes: a cached "ready" must not survive the
    daemon dying between the gate and the spawn."""
    import time

    monkeypatch.setattr(docker_sb, "_daemon_cache", (time.monotonic(), True))
    monkeypatch.setattr(docker_sb, "_run_docker", lambda *a, **k: None)
    assert docker_sb.daemon_reachable() is True
    assert docker_sb.daemon_reachable(use_cache=False) is False


def test_resolver_forwards_cache_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn passes ``use_cache=False``; pin the forwarding, not the call."""
    seen: dict[str, bool] = {}

    def _check(*, use_cache: bool = True) -> str | None:
        seen["use_cache"] = use_cache
        return None

    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
    monkeypatch.setattr(docker_sb, "check_docker_sandbox", _check)
    assert docker_sb.resolve_adapter_confinement(ACP_BACKEND_OPENCODE, "auto") == "docker"
    assert seen["use_cache"] is True
    assert docker_sb.resolve_adapter_confinement(ACP_BACKEND_OPENCODE, "auto", False) == "docker"
    assert seen["use_cache"] is False


def test_resolver_prefers_the_native_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker is fallback confinement, never a replacement: where the Crew OS
    mask applies, the established wrap path wins even with the flag on."""
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
    monkeypatch.setattr(docker_sb, "sandbox_docker_enabled", lambda: True)
    monkeypatch.setattr(docker_sb, "daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(docker_sb, "daemon_ostype", lambda *a, **k: "linux")
    monkeypatch.setattr(docker_sb, "image_available", lambda *a, **k: True)
    assert docker_sb.resolve_adapter_confinement(ACP_BACKEND_OPENCODE, "auto") == "native"


def test_resolver_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
    monkeypatch.setattr(docker_sb, "sandbox_docker_enabled", lambda: False)
    assert docker_sb.resolve_adapter_confinement(ACP_BACKEND_OPENCODE, "auto") == "refused"
    assert docker_sb.resolve_adapter_confinement(ACP_BACKEND_CODEX, "auto") == "refused"


def test_auth_file_absent_reads_as_no_bind(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert docker_sb.host_auth_file() is None
    auth_dir = tmp_path / ".local" / "share" / "opencode"
    auth_dir.mkdir(parents=True)
    auth = auth_dir / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    assert docker_sb.host_auth_file() == str(auth)


def test_auth_file_honours_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``XDG_DATA_HOME`` replaces the ``.local/share`` prefix, so the override
    spelling wins over the ``$HOME``-rooted default."""
    home_auth_dir = tmp_path / "home" / ".local" / "share" / "opencode"
    home_auth_dir.mkdir(parents=True)
    home_auth = home_auth_dir / "auth.json"
    home_auth.write_text("{}", encoding="utf-8")
    xdg_auth_dir = tmp_path / "xdg" / "opencode"
    xdg_auth_dir.mkdir(parents=True)
    xdg_auth = xdg_auth_dir / "auth.json"
    xdg_auth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert docker_sb.host_auth_file() == str(xdg_auth)


def test_config_dir_honours_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert docker_sb.host_config_dir() is None
    xdg_config = tmp_path / "xdg" / "opencode"
    xdg_config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert docker_sb.host_config_dir() == str(xdg_config)


def test_state_dir_lives_under_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path / "crew")
    state = docker_sb.sandbox_state_dir()
    assert state == str(tmp_path / "crew" / "opencode-sandbox" / "state")
    assert docker_sb.ensure_sandbox_state_dir() == state
    assert (tmp_path / "crew" / "opencode-sandbox" / "state").is_dir()


def test_shipped_dockerfile_exists() -> None:
    dockerfile = docker_sb.dockerfile_path()
    assert dockerfile.is_file(), dockerfile
    text = dockerfile.read_text(encoding="utf-8")
    assert "opencode-ai" in text
    assert docker_sb.IMAGE_REF in docker_sb.build_command()


def test_refusal_names_the_unmet_docker_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must say WHICH piece is missing, not just name the flag."""
    from kiro_crew import platform_compat, sandbox
    from kiro_crew.agent_sdk import tool_gate as gate

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
    monkeypatch.setattr(docker_sb, "resolve_adapter_confinement", lambda backend, mode: "refused")
    monkeypatch.setattr(
        docker_sb,
        "check_docker_sandbox",
        lambda: "agent.sandbox_docker is not enabled",
    )
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_sandbox_floor(ACP_BACKEND_OPENCODE, "auto")
    msg = str(excinfo.value)
    assert "Docker status" in msg
    assert "sandbox_docker is not enabled" in msg


def test_permit_when_docker_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import platform_compat, sandbox
    from kiro_crew.agent_sdk import tool_gate as gate

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
    monkeypatch.setattr(docker_sb, "resolve_adapter_confinement", lambda backend, mode: "docker")
    gate.enforce_sandbox_floor(ACP_BACKEND_OPENCODE, "auto")  # must not raise


def test_wire_cwd_names_the_container_path_when_confined() -> None:
    """A host ``C:\\...`` cwd names nothing on the container's filesystem, so
    session/new|load fail inside the harness. Both wire sites must send the
    container workdir when the spawn arm resolved Docker confinement."""
    from kiro_crew.acp.client import AcpClient

    for method in (
        AcpClient._new_session_following_substitution,
        AcpClient._initialize_session,
    ):
        assert "self._docker_wire_cwd or await self._session_work_dir()" in inspect.getsource(
            method
        ), method.__name__
