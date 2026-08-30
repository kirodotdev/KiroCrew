"""OpenCode's executable/catalog seams use fake discovery and subprocesses only."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client, opencode
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.drivers import acp as driver


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    monkeypatch.delenv(opencode.OPENCODE_BIN_ENV, raising=False)
    monkeypatch.setattr(opencode.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client, "_mise_which", lambda _name: None)
    monkeypatch.setattr(client, "_normalize_exe_casing", lambda path: path)
    monkeypatch.setattr(opencode.platform_compat, "is_executable_file", lambda _path: False)
    monkeypatch.setattr(opencode.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(opencode, "augmented_path", lambda _path: "fake-daemon-path")
    return tmp_path


def test_override_preserves_launch_path(discovery, monkeypatch):
    executable = discovery / "shim" / opencode.OPENCODE_BIN
    monkeypatch.setenv(opencode.OPENCODE_BIN_ENV, str(executable))
    monkeypatch.setattr(
        opencode.platform_compat, "is_executable_file", lambda path: Path(path) == executable
    )
    monkeypatch.setattr(
        client, "_mise_which", MagicMock(side_effect=AssertionError("override wins"))
    )
    assert opencode.resolve_opencode_bin() == str(executable)


def test_mise_precedes_install_home(discovery, monkeypatch):
    executable = str(discovery / "mise-opencode")
    monkeypatch.setattr(client, "_mise_which", lambda _name: executable)
    monkeypatch.setattr(opencode.platform_compat, "is_executable_file", lambda _path: True)
    assert opencode.resolve_opencode_bin() == executable


def test_installer_home_precedes_augmented_path(discovery, monkeypatch):
    name = "opencode.exe" if opencode.platform_compat.IS_WINDOWS else "opencode"
    executable = discovery / ".opencode" / "bin" / name
    monkeypatch.setattr(
        opencode.platform_compat, "is_executable_file", lambda path: Path(path) == executable
    )
    assert opencode.resolve_opencode_bin() == str(executable)


def test_augmented_path_is_last_discovery_step(discovery, monkeypatch):
    executable = str(discovery / "path-opencode")
    which = MagicMock(return_value=executable)
    monkeypatch.setattr(opencode.shutil, "which", which)
    assert opencode.resolve_opencode_bin() == executable
    which.assert_called_once_with(opencode.OPENCODE_BIN, path="fake-daemon-path")


def test_missing_executable_returns_none(discovery):
    assert opencode.resolve_opencode_bin() is None


@pytest.mark.parametrize("present", [True, False])
def test_install_probe_reuses_the_spawn_resolver(monkeypatch, present):
    monkeypatch.setattr(
        opencode, "resolve_opencode_bin", lambda: "fake-opencode" if present else None
    )
    backend_install.clear_probe_cache()
    try:
        state = backend_install.probe_backend(ACP_BACKEND_OPENCODE)
        assert state.installed == (
            backend_install.INSTALLED if present else backend_install.MISSING
        )
        assert state.policy_id == "opencode"
        assert state.missing_components == (
            () if present else (backend_install.COMPONENT_OPENCODE_CLI,)
        )
        assert state.install_command == ("" if present else f"npm i -g {opencode.OPENCODE_NPM_PKG}")
        assert state.restart_required is False
    finally:
        backend_install.clear_probe_cache()


def test_install_probe_failure_is_unknown_not_missing(monkeypatch):
    monkeypatch.setattr(
        opencode, "resolve_opencode_bin", MagicMock(side_effect=OSError("unreadable"))
    )
    backend_install.clear_probe_cache()
    try:
        state = backend_install.probe_backend(ACP_BACKEND_OPENCODE)
        assert state.installed == backend_install.UNKNOWN
        assert state.missing_components == ()
        assert state.install_command == ""
    finally:
        backend_install.clear_probe_cache()


def test_catalog_rejects_oversized_output(monkeypatch):
    monkeypatch.setattr(opencode, "MODEL_LIST_MAX_BYTES", 3)
    with pytest.raises(ValueError, match="output limit"):
        opencode.model_rows(b"p/m\n")


def test_catalog_rejects_invalid_encoding():
    with pytest.raises(UnicodeDecodeError):
        opencode.model_rows(b"provider/\xff\n")


def test_catalog_rejects_terminal_control_sequences():
    with pytest.raises(ValueError, match="invalid model id"):
        opencode.model_rows(b"\x1b[32mprovider/model\x1b[0m\n")


@pytest.fixture
def fake_catalog_process(monkeypatch, tmp_path):
    from kiro_crew import sandbox

    proc = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"provider/model\n", b""])),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
        wait=AsyncMock(return_value=0),
        returncode=0,
    )
    spawn = AsyncMock(return_value=proc)
    reap = AsyncMock()
    discard = MagicMock()
    wrap = MagicMock(return_value=(["wrapped-opencode", "models"], "fake-profile"))
    monkeypatch.setattr(opencode, "resolve_opencode_bin", lambda: str(tmp_path / "opencode"))
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "wrap_argv", wrap)
    monkeypatch.setattr(opencode, "_model_list_env", lambda: {"PATH": "fake-path"})
    monkeypatch.setattr(opencode, "_discard_sandbox", discard)
    monkeypatch.setattr(sandbox, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(sandbox, "create_subprocess_limited", spawn)
    monkeypatch.setattr(opencode.platform_compat, "kill_and_reap", reap)
    return proc, spawn, reap, discard, wrap


@pytest.mark.asyncio
async def test_catalog_spawn_stays_fixed_and_returns_plain_data(fake_catalog_process, tmp_path):
    proc, spawn, reap, discard, wrap = fake_catalog_process
    rows = await driver.query_opencode_models(work_dir=str(tmp_path))
    assert rows == [
        {"model_name": "provider/model", "display_name": "provider/model", "description": ""}
    ]
    wrap.assert_called_once_with(
        [str(tmp_path / "opencode"), "models"],
        mode="strict",
        strip_python_env=True,
        is_kiro_cli=False,
    )
    assert spawn.call_args.args == ("wrapped-opencode", "models")
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert spawn.call_args.kwargs["env"] == {"PATH": "fake-path"}
    proc.wait.assert_awaited_once()
    reap.assert_not_awaited()
    discard.assert_called_once_with("fake-profile")


@pytest.mark.asyncio
async def test_catalog_nonzero_status_does_not_become_a_model(fake_catalog_process, tmp_path):
    proc, _spawn, _reap, discard, _wrap = fake_catalog_process
    proc.returncode = 1
    with pytest.raises(RuntimeError, match="command failed"):
        await driver.query_opencode_models(work_dir=str(tmp_path))
    discard.assert_called_once_with("fake-profile")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError, asyncio.CancelledError])
async def test_catalog_failure_reaps_child_and_discards_profile(
    fake_catalog_process, monkeypatch, tmp_path, failure
):
    proc, _spawn, reap, discard, _wrap = fake_catalog_process
    monkeypatch.setattr(opencode, "_model_list_output", AsyncMock(side_effect=failure()))
    with pytest.raises(failure):
        await driver.query_opencode_models(work_dir=str(tmp_path))
    reap.assert_awaited_once_with(proc)
    discard.assert_called_once_with("fake-profile")


@pytest.mark.asyncio
async def test_catalog_stream_retention_is_bounded(fake_catalog_process, monkeypatch):
    proc, *_rest = fake_catalog_process
    monkeypatch.setattr(opencode, "MODEL_LIST_MAX_BYTES", 3)
    monkeypatch.setattr(opencode, "_MODEL_LIST_STDERR_TAIL_BYTES", 2)
    proc.stdout.read = AsyncMock(side_effect=[b"12345678", b"more", b""])
    proc.stderr.read = AsyncMock(side_effect=[b"diagnostic", b"tail", b""])
    out, err = await opencode._model_list_output(proc)
    assert out == b"1234"
    assert err == b"il"
    with pytest.raises(ValueError, match="output limit"):
        opencode.model_rows(out)


@pytest.mark.asyncio
async def test_cancel_during_sandbox_preparation_retires_the_late_profile(
    fake_catalog_process, monkeypatch, tmp_path
):
    from kiro_crew import sandbox

    _proc, spawn, _reap, _discard, _wrap = fake_catalog_process
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    profile = tmp_path / "late-profile.sb"

    def prepare(argv, **_kwargs):
        profile.write_text("inert test profile", encoding="utf-8")
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=5):
            raise AssertionError("test did not release the sandbox worker")
        return argv, str(profile)

    monkeypatch.setattr(sandbox, "wrap_argv", prepare)
    task = asyncio.create_task(driver.query_opencode_models(work_dir=str(tmp_path)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert not profile.exists()
        spawn.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_catalog_env_drops_kiro_only_credential(monkeypatch):
    from kiro_crew import sandbox

    monkeypatch.setenv("KIRO_API_KEY", "fake-kiro-only-key")
    monkeypatch.setattr(opencode, "augmented_path", lambda _path: "fake-path")
    monkeypatch.setattr(client, "_resolve_ssh_auth_sock", lambda _env: None)
    monkeypatch.setattr(sandbox, "scrub_agent_subprocess_env", lambda env: env)
    env = opencode._model_list_env()
    assert "KIRO_API_KEY" not in env
    assert env["PATH"] == "fake-path"


@pytest.mark.parametrize("root", ["relative-data", ".", "../data", "~/data", "~", " "])
def test_auth_home_refuses_cwd_dependent_or_tilde_override(root):
    with pytest.raises(ValueError, match="XDG_DATA_HOME must be an absolute path") as error:
        opencode.validate_opencode_env({"XDG_DATA_HOME": root})
    assert str(error.value) == "OpenCode XDG_DATA_HOME must be an absolute path without NUL bytes"


@pytest.mark.parametrize("suffix", ["data", "data ", "literal~data"])
def test_auth_home_preserves_absolute_paths_verbatim(tmp_path, suffix):
    env = {"XDG_DATA_HOME": str(tmp_path / suffix), "OPENCODE_AUTH_CONTENT": "opaque-fixture"}
    before = dict(env)
    assert opencode.validate_opencode_env(env) is None
    assert env == before


@pytest.mark.parametrize("env", [{}, {"XDG_DATA_HOME": ""}])
def test_auth_home_accepts_the_native_default(env):
    assert opencode.validate_opencode_env(env) is None


def test_auth_home_refuses_nul_without_echoing_the_value(tmp_path):
    with pytest.raises(ValueError, match="without NUL bytes") as error:
        opencode.validate_opencode_env({"XDG_DATA_HOME": str(tmp_path / "private") + "\x00tail"})
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_catalog_refuses_relocated_auth_before_spawning(
    fake_catalog_process, monkeypatch, tmp_path
):
    _proc, spawn, _reap, discard, _wrap = fake_catalog_process
    monkeypatch.setattr(opencode, "_model_list_env", lambda: {"XDG_DATA_HOME": "~/other-home"})
    with pytest.raises(ValueError, match="XDG_DATA_HOME must be an absolute path"):
        await driver.query_opencode_models(work_dir=str(tmp_path))
    spawn.assert_not_awaited()
    discard.assert_called_once_with("fake-profile")


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_override", [False, True])
async def test_dormant_spawn_validates_auth_home_before_binary_or_sandbox(
    monkeypatch, tmp_path, extra_override
):
    # This is dormant preparation, not session admission: no constructor guard
    # is patched and every process boundary is a fail-if-reached sentinel.
    dormant = object.__new__(client.AcpClient)
    dormant._acp_backend = ACP_BACKEND_OPENCODE
    dormant._work_dir = tmp_path
    dormant._extra_env = {"XDG_DATA_HOME": "~/other-data"} if extra_override else {}
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path) if extra_override else "relative-data")
    resolve = MagicMock(side_effect=AssertionError("binary resolution reached"))
    wrap = AsyncMock(side_effect=AssertionError("sandbox preparation reached"))
    spawn = AsyncMock(side_effect=AssertionError("native spawn reached"))
    monkeypatch.setattr(client, "_resolve_opencode_bin", resolve)
    monkeypatch.setattr(client, "wrap_argv_async", wrap)
    monkeypatch.setattr(client, "create_subprocess_limited", spawn)
    with pytest.raises(client.AcpError, match="XDG_DATA_HOME must be an absolute path") as error:
        await dormant._spawn()
    assert error.value.transient is False
    resolve.assert_not_called()
    wrap.assert_not_awaited()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["strict", "workspace", "off"])
async def test_catalog_wrap_never_delegates_to_kiro_sandbox(
    fake_catalog_process, monkeypatch, tmp_path, mode
):
    from kiro_crew import sandbox

    _proc, _spawn, _reap, _discard, wrap = fake_catalog_process
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: mode)
    await driver.query_opencode_models(work_dir=str(tmp_path))
    wrap.assert_called_once_with(
        [str(tmp_path / "opencode"), "models"],
        mode=mode,
        strip_python_env=True,
        is_kiro_cli=False,
    )
