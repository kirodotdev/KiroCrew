"""``AcpRuntime`` spawn and start-up.

The trusted ``kiro-cli`` resolver, cold-start admission, the wrapper chain the
installed path passes through, and the verified workspace a bound macOS runtime
hands its sessions.
"""

import asyncio
import gc
import os
import sys
import weakref
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _spawn_client_mod
from spawn_test_helpers import strip_spawn_shim

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError, _ColdStartAdmission
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import await_until


def test_runtime_uses_clients_augmented_kiro_bin_resolver():
    """Every spawn path must resolve kiro-cli via the SAME augmented-PATH resolver
    as AcpClient (honours KIROCREW_KIRO_BIN + augmented_path so a non-login
    gateway finds a ~/.local/bin install). A bare shutil.which(PATH) duplicate
    regressed the kiro/_bg path to 'kiro-cli not found in PATH'. Assert
    single-source.

    Read as SOURCE rather than by identity because each kiro-family harness
    resolves the binary at call time, which is what lets a test patch the
    resolver at its definition site instead of at a re-export that may not
    exist."""
    import inspect

    import kiro_crew.acp.client as client_mod
    from kiro_crew.acp.harness import KasHarness, KiroHarness

    assert hasattr(client_mod, "_resolve_kiro_bin_for_spawn")
    for harness in (KiroHarness, KasHarness):
        source = inspect.getsource(harness.resolve_spawn)
        assert "_resolve_kiro_bin_for_spawn" in source, harness.__name__
        assert "shutil.which" not in source, harness.__name__


@pytest.mark.parametrize("backend", [None, ACP_BACKEND_KAS])
@pytest.mark.asyncio
async def test_runtime_missing_kiro_bin_reports_the_directories_it_searched(backend):
    """The runtime spawn path must DIAGNOSE a missing Kiro CLI, not just name it.

    ``AcpClient._spawn`` already separates "this binary is not installed" from
    "its install directory is outside the search" by naming every directory it
    walked (see ``test_spawn_kiro_missing_bin_reports_only_resolver_search_dirs``
    and ``env.describe_search_path``). The runtime sibling raised a bare
    ``kiro-cli not found in PATH``, which is both less information and factually
    wrong: resolution also walks ``%LOCALAPPDATA%\\Kiro-Cli``,
    ``%ProgramFiles%\\Kiro-Cli`` and the ``KIROCREW_KIRO_BIN`` override, none of
    which is PATH.

    That gap misleads. A "not found in PATH" message invites running
    ``where kiro-cli``, getting nothing, seeing the gateway's OWN ``kirocrew.exe``
    in the app bundle, and concluding the agent CLI is renamed and this
    lookup is stale. It is not: ``kirocrew`` is Kiro Crew's own console
    script and ``kiro-cli`` is a separate prerequisite the message never said it
    was looking for anywhere but PATH.

    Both spawn branches are covered because both resolve the same binary and both
    have to answer the same question.
    """
    import kiro_crew.acp.client as client_mod

    searched = [os.path.join(os.sep, "managed-bin"), os.path.join(os.sep, "path-bin")]
    unsearched = os.path.join(os.sep, "never-checked")
    rt = AcpRuntime(work_dir="/tmp")
    if backend is not None:
        rt._acp_backend = backend

    async def _no_bin(*, environ=None, home=None):
        return None

    with (
        patch.object(client_mod, "_resolve_kiro_bin_for_spawn", _no_bin),
        patch.object(client_mod, "known_kiro_cli_dirs", return_value=searched),
    ):
        with pytest.raises(AcpRuntimeError) as raised:
            await rt._resolve_spawn_plan()

    message = str(raised.value)
    assert "searched 2 directories" in message
    assert searched[0] in message
    assert searched[1] in message
    assert unsearched not in message
    # The remedy travels with the diagnosis, the rule env.MCP_PATH_HINT exists
    # for: a reader who learns the search missed their install needs the one
    # knob that covers it named here, not only in the source.
    assert "KIROCREW_KIRO_BIN" in message


async def _wait_for_queued(admission: _ColdStartAdmission, expected: int) -> None:
    """Yield to scheduled starters until the coordinator exposes the queue."""
    for _ in range(100):
        if admission.queued == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"cold-start queue did not reach {expected}, active={admission.active}, "
        f"queued={admission.queued}"
    )


@pytest.mark.asyncio
async def test_cold_start_admission_caps_simultaneous_runtime_spawns(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=2)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    release = asyncio.Event()
    first_two_entered = asyncio.Event()
    running = 0
    peak = 0

    async def controlled_spawn(self):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        if running == 2:
            first_two_entered.set()
        try:
            await release.wait()
        finally:
            running -= 1

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", controlled_spawn)
    tasks = [asyncio.create_task(AcpRuntime().spawn()) for _ in range(3)]
    try:
        await await_until(first_two_entered, what="the first two spawns entering")
        await _wait_for_queued(admission, 1)

        assert peak == 2
        assert admission.active == 2
        assert admission.queued == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 2
    assert admission.active == 0
    assert admission.queued == 0


@pytest.mark.asyncio
async def test_cold_start_cancellation_releases_active_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    block = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await block.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    task = asyncio.create_task(AcpRuntime().spawn())
    try:
        await await_until(entered, what="the spawn entering")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert admission.active == 0
    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_queued_cancellation_does_not_consume_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_spawn(self):
        entered.set()
        await release.wait()

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", blocked_spawn)
    active = asyncio.create_task(AcpRuntime().spawn())
    queued = None
    try:
        await await_until(entered, what="the spawn entering")
        queued = asyncio.create_task(AcpRuntime().spawn())
        await _wait_for_queued(admission, 1)

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert admission.active == 1
        assert admission.queued == 0
    finally:
        release.set()
        if queued is not None and not queued.done():
            queued.cancel()
        cleanup_tasks = [active] + ([] if queued is None else [queued])
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    assert admission.active == 0


@pytest.mark.asyncio
async def test_cold_start_exception_releases_admission_slot(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    admission = _ColdStartAdmission(limit=1)
    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: admission)

    async def fail_spawn(self):
        raise RuntimeError("initialize failed")

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", fail_spawn)
    with pytest.raises(RuntimeError, match="initialize failed"):
        await AcpRuntime().spawn()
    assert admission.active == 0

    monkeypatch.setattr(AcpRuntime, "_spawn_admitted", AsyncMock(return_value=None))
    await AcpRuntime().spawn()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_runtime_spawn_retries_one_creation_failure_after_backoff(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    process = MagicMock()
    sleep = AsyncMock()
    create = AsyncMock(side_effect=[FileNotFoundError("kiro-cli is being replaced"), process])
    monkeypatch.setattr(runtime_mod.asyncio, "sleep", sleep)

    assert await runtime_mod._retrying_spawn_factory(create) is process

    assert create.await_count == 2
    sleep.assert_awaited_once_with(runtime_mod._ACP_RUNTIME_RESPAWN_BACKOFF_S)


def test_cold_start_admission_registry_releases_contended_closed_loop(monkeypatch):
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setattr(
        runtime_mod,
        "_cold_start_admissions",
        runtime_mod.weakref.WeakKeyDictionary(),
    )
    monkeypatch.setattr(runtime_mod, "_COLD_START_MAX_CONCURRENT", 1)

    loop = asyncio.new_event_loop()
    loop_ref = weakref.ref(loop)
    asyncio.set_event_loop(loop)

    async def contend_and_drain():
        admission = runtime_mod._cold_start_admission()
        assert runtime_mod._cold_start_admission() is admission
        await admission.acquire(StartPriority.BACKGROUND)
        queued = asyncio.create_task(admission.acquire(StartPriority.BACKGROUND))
        await _wait_for_queued(admission, 1)
        admission_ref = weakref.ref(admission)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        admission.release(StartPriority.BACKGROUND)
        assert admission.active == 0
        assert admission.queued == 0
        return admission_ref

    try:
        admission_ref = loop.run_until_complete(contend_and_drain())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
        del loop

    gc.collect()
    assert admission_ref() is None
    assert loop_ref() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("project_skills", [True, False])
async def test_runtime_spawn_passes_installed_path_through_exact_wrappers(
    tmp_path,
    monkeypatch,
    project_skills,
):
    import kiro_crew.acp.runtime as runtime_mod

    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "1" if project_skills else "0")
    macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    executable = macos_dir / "kiro-cli"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")
    launch_path = str(executable)
    wrapped: dict[str, object] = {}

    class _StopSpawn(Exception):
        pass

    def capture_wrap(argv, mode, **kwargs):
        wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
        return ["/usr/bin/sandbox-wrapper", *argv], None

    async def stop_spawn(*args, **kwargs):
        wrapped["spawn_args"] = args
        wrapped["spawn_kwargs"] = kwargs
        # Read while the spawn is still in flight: its failure path closes the
        # bound workspace descriptor and clears the attribute.
        wrapped["bound_fd"] = runtime._bound_workspace_fd
        raise _StopSpawn()

    async def resolve_installed(*, environ=None, home=None):
        return launch_path

    client_mod = _spawn_client_mod()
    monkeypatch.setattr(
        client_mod,
        "_resolve_kiro_bin_for_spawn",
        resolve_installed,
    )
    monkeypatch.setattr(runtime_mod, "wrap_argv", capture_wrap)
    voice_guard = MagicMock()
    monkeypatch.setattr(runtime_mod, "assert_voice_runtime_outside_agent_workspace", voice_guard)
    monkeypatch.setattr(
        runtime_mod,
        "cgroup_scope_argv",
        lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
    )
    # The claim below is about the SNAPSHOT descriptor (there must be none, the
    # binary is exec'd in place). On macOS the darwin-only workspace binding adds
    # its own descriptor to pass_fds, so pin that seam to the no-descriptor shape
    # every other platform already produces, or the assertion reads the wrong fd.

    async def _unbound_workspace(work_dir):
        return work_dir, None

    monkeypatch.setattr(runtime_mod, "bind_voice_safe_agent_workspace_async", _unbound_workspace)
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", stop_spawn)

    runtime = AcpRuntime(work_dir=tmp_path / "workspace")
    with pytest.raises(_StopSpawn):
        await runtime.spawn()

    if project_skills:
        native_agent = runtime._native_skill_projection.spawn_agent(runtime._agent)
    else:
        assert runtime._native_skill_projection is None
        native_agent = runtime._agent
    assert wrapped["argv"] == [launch_path, "acp", "--agent", native_agent]
    assert wrapped["mode"] == "auto"
    wrap_kwargs = dict(wrapped["kwargs"])
    # The per-process scratch window is allocated at spawn time; its path is
    # runtime-owned, so only its presence and shape are pinned here.
    extra_private = wrap_kwargs.pop("extra_private_dirs")
    assert isinstance(extra_private, (list, tuple))
    # The default tier leaves ~/.ssh readable, so the ssh-agent socket is kept.
    assert wrap_kwargs == {
        "strip_python_env": True,
        "is_kiro_cli": True,
        **({} if sys.platform == "win32" else {"forward_ssh_auth_sock": True}),
    }
    voice_guard.assert_called_once_with(runtime._work_dir)
    assert strip_spawn_shim(wrapped["spawn_args"]) == (
        "/usr/bin/cgroup-wrapper",
        "/usr/bin/sandbox-wrapper",
        launch_path,
        "acp",
        "--agent",
        native_agent,
    )
    spawn_kwargs = wrapped["spawn_kwargs"]
    assert isinstance(spawn_kwargs, dict)
    # The installed binary is exec'd in place: the ONLY descriptor handed to the
    # child is the verified workspace the spawn shim must `fchdir` into, never an
    # inherited snapshot descriptor. Nothing binds a workspace off macOS, so the
    # expected set is empty there -- asserting the exact set rather than the absence
    # of the key keeps the same strength on Linux and stops pinning the platform's
    # own spawn shape on darwin.
    bound_fd = wrapped["bound_fd"]
    expected_fds: tuple[int, ...] = () if bound_fd is None else (bound_fd,)
    assert tuple(spawn_kwargs.get("pass_fds", ())) == expected_fds
    # The sibling subcommand binary a multi-call CLI dispatches to is still
    # reachable beside the launch path.
    assert (Path(launch_path).parent / "kiro-cli-chat").exists()


@pytest.mark.asyncio
async def test_bound_macos_runtime_hands_sessions_the_verified_pathname(monkeypatch, tmp_path):
    """The ACP peer resolves this name itself, so it must be a name it can resolve.

    It is returned only after the descriptor proves it still names the bound
    identity. The earlier "/dev/fd/<n>" spelling is not resolvable on macOS -- the
    only platform that binds -- so it reached the peer as an unusable cwd.
    """
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    target = AsyncMock(return_value="/canonical/workspace")
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    # The DESCRIPTOR's own name, not the pathname the caller asked with: the peer
    # re-resolves what it is handed, so the caller's spelling would leave the
    # retarget window open.
    assert await runtime._session_work_dir(tmp_path) == "/canonical/workspace"
    target.assert_called_once_with(71, tmp_path)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_descendant_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 71
    runtime._spawn_work_dir = str(tmp_path)
    descendant = tmp_path / "packages" / "app"
    target = AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch(str(descendant)))
    monkeypatch.setattr(runtime_mod, "resolve_bound_session_workspace", target)

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(descendant)
    target.assert_awaited_once_with(71, descendant)


@pytest.mark.asyncio
async def test_bound_macos_runtime_rejects_different_session_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 72
    runtime._spawn_work_dir = str(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "resolve_bound_session_workspace",
        AsyncMock(side_effect=runtime_mod.BoundWorkspaceMismatch("retargeted")),
    )

    with pytest.raises(AcpRuntimeError, match="exact workspace"):
        await runtime._session_work_dir(tmp_path / "retargeted")


@pytest.mark.asyncio
async def test_kill_cancellation_still_releases_bound_workspace(monkeypatch, tmp_path):
    import kiro_crew.acp.runtime as runtime_mod

    runtime = AcpRuntime(work_dir=tmp_path)
    runtime._bound_workspace_fd = 73
    runtime._spawn_work_dir = str(tmp_path)
    entered = asyncio.Event()
    closed: list[int] = []

    async def stalled_teardown(*, expected=False, reason=""):
        entered.set()
        await asyncio.Event().wait()

    async def record_release(descriptor):
        closed.append(descriptor)

    monkeypatch.setattr(runtime, "_kill_inner", stalled_teardown)
    monkeypatch.setattr(runtime_mod, "release_bound_agent_workspace", record_release)

    task = asyncio.create_task(runtime.kill())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == [73]
    assert runtime._bound_workspace_fd is None
    assert runtime._spawn_work_dir == str(tmp_path)
