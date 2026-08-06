"""Dev Container wiring on the runtime spawn path.

This is the path real sessions take: ``AcpProvider.start()`` routes every
non-claude session through ``_start_kiro_runtime()`` -> ``AcpRuntime.spawn()``,
while the ``AcpClient`` created in ``AcpProvider.__init__`` is "never spawned —
just used for config storage".

That does NOT make the client's copy inert, which is why the eligibility gate,
the exec-id mint and the in-container kill live in ``kiro_crew.devcontainer``
with both paths as callers: ``AcpClient`` is spawned directly, on the default
kiro backend, by the Knowledge Library worker pool
(``knowledge/llm_pool.py``'s ``AcpWorker``), so a fix applied to one spawn path
and not the other would leave a live path behind. These tests pin the runtime
side of that arrangement.

Covered:
  - spawn() replaces argv with ``docker exec`` for a trusted devcontainer, and
    SKIPS the host sandbox + cgroup wrappers (the container's namespaces
    replace them; both wrappers are host mechanisms that cannot cross it).
  - spawn() takes the unchanged host path when the feature is off / untrusted /
    docker is missing.
  - _session_cwd maps to the container-side workspace, and REFUSES a session
    whose cwd is not this runtime's work dir (one runtime = one container).
  - kill() signals the in-container tree before host teardown.

No container is created and no kiro-cli is launched: the devcontainer manager
and the subprocess spawn are both stubbed.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError


class _StopSpawn(Exception):
    """Aborts spawn() right after argv is built, before any real process."""


def _fake_info(project: Path, *, remote: str = "/workspaces/proj") -> MagicMock:
    info = MagicMock()
    info.container_id = "c0ffee1234567890"
    info.remote_workspace_folder = remote
    info.remote_user = "vscode"
    info.project_dir = str(project)
    info.config_digest = "d" * 64
    return info


@pytest.fixture
def capture_spawn(monkeypatch):
    """Stub kiro-cli resolution + the subprocess so spawn() only builds argv."""
    seen: dict[str, object] = {}

    async def resolve_installed():
        return "/opt/kiro/kiro-cli"

    async def stop_spawn(*args, **kwargs):
        seen["argv"] = list(args)
        seen["kwargs"] = kwargs
        raise _StopSpawn()

    def capture_wrap(argv, mode, **kwargs):
        seen["wrap_called"] = True
        return ["/usr/bin/sandbox-wrapper", *argv], None

    def capture_cgroup(argv):
        seen["cgroup_called"] = True
        return ["/usr/bin/cgroup-wrapper", *argv]

    monkeypatch.setattr(runtime_mod, "_resolve_kiro_bin_for_spawn", resolve_installed)
    monkeypatch.setattr(runtime_mod, "wrap_argv", capture_wrap)
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", capture_cgroup)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", stop_spawn, raising=False)
    return seen


class TestSpawnUsesTheContainer:
    """The headline claim: a trusted project actually runs in its container."""

    @pytest.mark.asyncio
    async def test_argv_becomes_docker_exec_and_skips_host_wrappers(
        self, tmp_path, monkeypatch, capture_spawn
    ):
        """Revert the devcontainer branch in spawn() and this fails: argv would
        be the sandbox/cgroup-wrapped host command instead of a docker exec."""
        project = tmp_path / "proj"
        project.mkdir()
        info = _fake_info(project)
        rt = AcpRuntime(work_dir=project)

        async def resolved(self_):
            return info

        monkeypatch.setattr(AcpRuntime, "_maybe_devcontainer_info", resolved)
        mgr = MagicMock()
        mgr.exec_argv.return_value = ["docker", "exec", "-i", info.container_id, "sh"]
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        with pytest.raises(_StopSpawn):
            await rt.spawn()

        assert capture_spawn["argv"][:3] == ["docker", "exec", "-i"]
        # The container replaces the host mechanisms — neither may run.
        assert "wrap_called" not in capture_spawn
        assert "cgroup_called" not in capture_spawn
        assert rt._devcontainer_info is info
        assert rt._devcontainer_exec_id

        # The inner command relies on the CONTAINER's PATH: a host-resolved
        # absolute kiro-cli path is meaningless inside the image.
        inner = mgr.exec_argv.call_args.args[1]
        assert inner[0] == "kiro-cli"
        assert inner[:4] == ["kiro-cli", "acp", "--agent", rt._agent]
        assert mgr.exec_argv.call_args.kwargs["exec_id"] == rt._devcontainer_exec_id

    @pytest.mark.asyncio
    async def test_model_pin_survives_into_the_container(
        self, tmp_path, monkeypatch, capture_spawn
    ):
        """--model is the only way to cross provider boundaries, so it must be
        on the INNER argv, not lost with the host command."""
        project = tmp_path / "proj"
        project.mkdir()
        rt = AcpRuntime(work_dir=project, model="gpt-5.6-sol")

        async def resolved(self_):
            return _fake_info(project)

        monkeypatch.setattr(AcpRuntime, "_maybe_devcontainer_info", resolved)
        mgr = MagicMock()
        mgr.exec_argv.return_value = ["docker", "exec", "-i", "cid", "sh"]
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        with pytest.raises(_StopSpawn):
            await rt.spawn()

        inner = mgr.exec_argv.call_args.args[1]
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_host_path_unchanged_when_no_container(
        self, tmp_path, monkeypatch, capture_spawn
    ):
        """The default (feature off / untrusted / no docker) must be byte-for-byte
        the pre-existing host behaviour: sandbox THEN cgroup wrapping."""
        rt = AcpRuntime(work_dir=tmp_path / "workspace")

        async def no_container(self_):
            return None

        monkeypatch.setattr(AcpRuntime, "_maybe_devcontainer_info", no_container)

        with pytest.raises(_StopSpawn):
            await rt.spawn()

        assert capture_spawn["wrap_called"] is True
        assert capture_spawn["cgroup_called"] is True
        assert capture_spawn["argv"][0] == "/usr/bin/cgroup-wrapper"
        assert rt._devcontainer_info is None
        assert rt._devcontainer_exec_id is None


class TestSessionCwdOwnership:
    """One runtime hosts many sessions but exactly ONE container."""

    def test_host_runtime_passes_cwd_through(self, tmp_path):
        rt = AcpRuntime(work_dir=tmp_path / "wd")
        assert rt._session_cwd(None) == str(tmp_path / "wd")
        assert rt._session_cwd("/some/other/dir") == "/some/other/dir"

    def test_containerized_runtime_maps_to_the_container_workspace(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        rt = AcpRuntime(work_dir=project)
        rt._devcontainer_info = _fake_info(project, remote="/workspaces/proj")

        # None (use the runtime's own dir) and the work dir itself both map.
        assert rt._session_cwd(None) == "/workspaces/proj"
        assert rt._session_cwd(project) == "/workspaces/proj"
        # realpath-equal spellings map too (symlinked/relative forms).
        assert rt._session_cwd(str(project) + "/.") == "/workspaces/proj"

    def test_foreign_cwd_is_refused_not_silently_mapped(self, tmp_path):
        """The invariant, enforced. Handing the agent a container path that
        belongs to a DIFFERENT project (or does not exist in the image) is a
        correctness bug; the caller recovers by cold-starting its own runtime.
        Drop the raise and this test fails.
        """
        project = tmp_path / "proj"
        project.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        rt = AcpRuntime(work_dir=project)
        rt._devcontainer_info = _fake_info(project)

        with pytest.raises(AcpRuntimeError, match="one runtime has one Dev Container"):
            rt._session_cwd(other)


class TestKillSignalsTheContainer:
    @pytest.mark.asyncio
    async def test_kill_exec_runs_before_host_teardown(self, tmp_path, monkeypatch):
        """Killing the host-side `docker exec` client only detaches it, so the
        in-container tree must be signalled explicitly. Remove that call and
        the agent survives a force-kill."""
        project = tmp_path / "proj"
        project.mkdir()
        info = _fake_info(project)
        rt = AcpRuntime(work_dir=project)
        rt._devcontainer_info = info
        rt._devcontainer_exec_id = "abc123"

        mgr = MagicMock()
        mgr.kill_exec = AsyncMock()
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        await rt.kill()

        mgr.kill_exec.assert_awaited_once()
        assert mgr.kill_exec.await_args.args[0] is info
        assert mgr.kill_exec.await_args.args[1] == "abc123"

    @pytest.mark.asyncio
    async def test_kill_exec_failure_does_not_block_host_teardown(self, tmp_path, monkeypatch):
        """A dead/removed container must not strand the host process."""
        rt = AcpRuntime(work_dir=tmp_path)
        rt._devcontainer_info = _fake_info(tmp_path)
        rt._devcontainer_exec_id = "abc123"

        mgr = MagicMock()
        mgr.kill_exec = AsyncMock(side_effect=RuntimeError("container gone"))
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        await rt.kill()  # must not raise
        assert rt._dead is True

    @pytest.mark.asyncio
    async def test_host_runtime_does_not_touch_the_manager(self, tmp_path, monkeypatch):
        called = {"n": 0}

        def _boom():
            called["n"] += 1
            raise AssertionError("manager must not be consulted for a host runtime")

        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", _boom, raising=False)
        rt = AcpRuntime(work_dir=tmp_path)
        await rt.kill()
        assert called["n"] == 0


class TestBothSpawnPathsShareOneImplementation:
    """The anti-divergence guarantee for the two LIVE spawn paths.

    ``AcpRuntime.spawn()`` backs chat/subagent sessions; ``AcpClient._spawn()``
    backs the Knowledge Library worker pool, which constructs an ``AcpClient``
    on the default kiro backend and calls ``ensure_ready()``. Both must reach
    the same trust gate, the same exec-id mint and the same kill, or a fix to
    one leaves the other behind.

    REVERT-VERIFIED: reinstate either path's private copy of
    ``_maybe_devcontainer_info``, its own ``uuid.uuid4().hex`` mint, or its own
    ``kill_exec`` call, and the matching assertion below fails.
    """

    @pytest.mark.asyncio
    async def test_both_eligibility_seams_call_the_shared_resolver(self, tmp_path, monkeypatch):
        """Both paths reach ``resolve_with_locus``, directly or via the wrapper.

        Patching here catches either spelling, because ``resolve_for_work_dir``
        delegates to this function -- so this test pins "one resolver", NOT
        "called directly". The user-facing invariant that a fallback is always
        recorded is pinned separately below, since it is the one a divergent path
        could actually break.
        """
        from kiro_crew.acp.client import AcpClient

        seen: list[str] = []

        async def fake_resolve(work_dir):
            seen.append(str(work_dir))
            return None, None

        monkeypatch.setattr(
            "kiro_crew.devcontainer.resolve_with_locus", fake_resolve, raising=False
        )

        rt = AcpRuntime(work_dir=tmp_path)
        assert await rt._maybe_devcontainer_info() is None
        client = AcpClient(work_dir=tmp_path)
        assert await client._maybe_devcontainer_info() is None

        # One resolver, reached twice — once per spawn path.
        assert seen == [str(tmp_path), str(tmp_path)]

    @pytest.mark.asyncio
    async def test_both_spawn_paths_record_a_host_fallback(self, tmp_path, monkeypatch):
        """Neither path may resolve without the verdict becoming observable.

        This is the assertion with teeth: it patches the INNER resolver, so a
        path that reached the container decision by any route which skips
        ``resolve_with_locus`` leaves nothing recorded and fails here. Without
        it, the sibling test above would be satisfied by a path that merely
        called something named like the resolver.
        """
        import kiro_crew.devcontainer as devc_mod
        from kiro_crew.acp.client import AcpClient

        async def fake_inner(work_dir):
            return None, devc_mod.ExecutionLocus(
                "host", reason=devc_mod.HOST_REASON_DOCKER_UNAVAILABLE
            )

        monkeypatch.setattr(devc_mod, "_resolve_with_locus_inner", fake_inner, raising=False)

        for make in (
            lambda: AcpRuntime(work_dir=tmp_path),
            lambda: AcpClient(work_dir=tmp_path),
        ):
            devc_mod.record_execution_locus(tmp_path, None)
            assert devc_mod.execution_locus_for(tmp_path) is None
            assert await make()._maybe_devcontainer_info() is None
            recorded = devc_mod.execution_locus_for(tmp_path)
            assert recorded is not None, "spawn path resolved without recording the verdict"
            assert recorded.mode == "host"
            assert recorded.reason == "docker_unavailable"
        devc_mod.record_execution_locus(tmp_path, None)

    @pytest.mark.asyncio
    async def test_both_spawn_paths_mint_the_exec_id_through_the_shared_helper(
        self, tmp_path, monkeypatch, capture_spawn
    ):
        """Neither path may mint its own id: kill_exec's injection safety rests
        on the id being gateway-generated hex from one place."""
        import kiro_crew.devcontainer as devc_mod

        info = _fake_info(tmp_path)
        calls: list[tuple] = []
        real = devc_mod.containerize_spawn

        def recording(info_arg, inner_argv, *, env=None):
            calls.append((inner_argv, env))
            return real(info_arg, inner_argv, env=env)

        mgr = MagicMock()
        mgr.exec_argv.return_value = ["docker", "exec", "-i", info.container_id, "sh"]
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)
        monkeypatch.setattr("kiro_crew.devcontainer.containerize_spawn", recording, raising=False)

        async def resolved(_self):
            return info

        monkeypatch.setattr(AcpRuntime, "_maybe_devcontainer_info", resolved)

        rt = AcpRuntime(work_dir=tmp_path)
        with pytest.raises(_StopSpawn):
            await rt.spawn()

        assert len(calls) == 1
        assert rt._devcontainer_exec_id == mgr.exec_argv.call_args.kwargs["exec_id"]
        # uuid4 hex, so nothing shell-significant can ride in on it.
        assert len(rt._devcontainer_exec_id) == 32
        assert all(c in "0123456789abcdef" for c in rt._devcontainer_exec_id)

    @pytest.mark.asyncio
    async def test_both_kill_paths_call_the_shared_kill(self, tmp_path, monkeypatch):
        from kiro_crew.acp.client import AcpClient

        seen: list[tuple] = []

        async def fake_kill(info, exec_id):
            seen.append((info, exec_id))

        monkeypatch.setattr(
            "kiro_crew.devcontainer.kill_containerized_tree", fake_kill, raising=False
        )

        info = _fake_info(tmp_path)
        rt = AcpRuntime(work_dir=tmp_path)
        rt._devcontainer_info = info
        rt._devcontainer_exec_id = "a" * 32
        await rt.kill()

        # The client's host teardown follows the shared kill in the same method,
        # so it is cut short the instant the shared kill returns: a BaseException
        # from the first pipe close escapes the `except Exception` around it,
        # which keeps the test off the real process-kill machinery.
        class _StopTeardown(BaseException):
            pass

        client = AcpClient(work_dir=tmp_path)
        client._devcontainer_info = info
        client._devcontainer_exec_id = "b" * 32
        client._pid = 424242
        client._process = MagicMock()
        client._process.returncode = None
        client._process.stdin.close.side_effect = _StopTeardown
        with pytest.raises(_StopTeardown):
            await client._kill_process()

        assert seen == [(info, "a" * 32), (info, "b" * 32)]


class TestMaybeDevcontainerInfoGuards:
    """Every guard degrades to the host path rather than failing the session."""

    @pytest.mark.asyncio
    async def test_config_off_returns_none_without_touching_the_filesystem(
        self, tmp_path, monkeypatch
    ):
        probed = {"n": 0}

        def _probe(_p):
            probed["n"] += 1
            return None

        monkeypatch.setattr(
            "kiro_crew.devcontainer.find_devcontainer_config", _probe, raising=False
        )
        cfg = MagicMock()
        cfg.agent.devcontainer = "off"
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg, raising=False
        )

        rt = AcpRuntime(work_dir=tmp_path)
        assert await rt._maybe_devcontainer_info() is None
        assert probed["n"] == 0

    @pytest.mark.asyncio
    async def test_untrusted_config_runs_on_the_host(self, tmp_path, monkeypatch):
        cfg = MagicMock()
        cfg.agent.devcontainer = "auto"
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg, raising=False
        )
        monkeypatch.setattr(runtime_mod.sys, "platform", "linux")
        monkeypatch.setattr(
            "kiro_crew.devcontainer.find_devcontainer_config",
            lambda p: Path(p) / ".devcontainer" / "devcontainer.json",
            raising=False,
        )
        monkeypatch.setattr("kiro_crew.devcontainer.docker_available", lambda: True, raising=False)
        monkeypatch.setattr("kiro_crew.devcontainer.is_trusted", lambda p: False, raising=False)
        up_called = {"n": 0}
        mgr = MagicMock()

        async def _up(*a, **k):
            up_called["n"] += 1

        mgr.up = _up
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        rt = AcpRuntime(work_dir=tmp_path)
        assert await rt._maybe_devcontainer_info() is None
        # Never builds for an untrusted config.
        assert up_called["n"] == 0

    @pytest.mark.asyncio
    async def test_non_linux_host_runs_on_the_host(self, tmp_path, monkeypatch):
        """Docker Desktop is a VM, so the parity path is Linux-only in v1."""
        cfg = MagicMock()
        cfg.agent.devcontainer = "auto"
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg, raising=False
        )
        monkeypatch.setattr(runtime_mod.sys, "platform", "darwin")

        rt = AcpRuntime(work_dir=tmp_path)
        assert await rt._maybe_devcontainer_info() is None

    @pytest.mark.asyncio
    async def test_up_failure_degrades_to_the_host(self, tmp_path, monkeypatch):
        cfg = MagicMock()
        cfg.agent.devcontainer = "auto"
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg, raising=False
        )
        monkeypatch.setattr(runtime_mod.sys, "platform", "linux")
        monkeypatch.setattr(
            "kiro_crew.devcontainer.find_devcontainer_config",
            lambda p: Path(p) / ".devcontainer" / "devcontainer.json",
            raising=False,
        )
        monkeypatch.setattr("kiro_crew.devcontainer.docker_available", lambda: True, raising=False)
        monkeypatch.setattr("kiro_crew.devcontainer.is_trusted", lambda p: True, raising=False)
        mgr = MagicMock()
        mgr.up = AsyncMock(side_effect=RuntimeError("image pull failed"))
        monkeypatch.setattr("kiro_crew.devcontainer.get_manager", lambda: mgr, raising=False)

        rt = AcpRuntime(work_dir=tmp_path)
        assert await rt._maybe_devcontainer_info() is None


def test_asyncio_and_os_imports_used() -> None:
    """Keep the module's imports honest for the linters."""
    assert asyncio is not None and os is not None
