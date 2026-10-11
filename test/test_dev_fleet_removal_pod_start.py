"""A Dev Fleet worktree removal and a pod start on that worktree exclude each other.

Real: ``worktree_ops._worktree_remove`` and ``worktree_ops._pod_up``, a scratch git
repository under ``tmp_path`` with real worktrees, and a real ``git worktree remove``.
Stand-ins: the pod backend (``runtime.rt``, an in-memory set of active pod names) and
the ``kirocrew pod up`` CLI, which marks the pod active the way its unit does.

The two-process tests split the work the way production does: an agent's pod start
runs in the gateway (this process, serving the real removal-lease routes on
127.0.0.1), and the removal's lease is taken by a second process through the real
``GatewayPointerBroker`` the backend installs.
"""

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import kiro_crew
from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_routes,
    live,
    repository,
    runtime,
    worktree_ops,
)

NAME = "feature-wt"
OTHER = "other-wt"
BOUND = 10.0


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class _PodError(Exception):
    pass


class _Absent(_PodError):
    pass


class _Lease:
    refusal = None

    def __bool__(self) -> bool:
        return True


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=repo,
    )
    trees = {name: tmp_path / name for name in (NAME, OTHER)}
    for name, path in trees.items():
        _git("worktree", "add", "-q", "-b", f"feat/{name}", str(path), cwd=repo)

    active: set[str] = set()
    cfg = SimpleNamespace(pod_root=tmp_path / "pods")
    cfg.pod_root.mkdir()
    rt = SimpleNamespace(
        PodError=_PodError,
        PodBackendAbsent=_Absent,
        PodOwnershipUnproven=_PodError,
        require_backend=lambda: None,
        active_names=lambda c: set(active),
        orphan_homes=lambda c, *a, **k: [],
        pod_home=lambda c, n: cfg.pod_root / n,
    )

    async def find(name):
        path = trees.get(name)
        if path is not None and path.is_dir():
            return {"path": str(path), "branch": f"feat/{name}", "is_main": False}, None
        return None, f"unknown worktree: {name!r}"

    @asynccontextmanager
    async def removal_lease(path):
        yield _Lease()

    monkeypatch.setattr(runtime, "_RUNS", {})
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {})
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(fleet_state, "_OWNER_REPO", None)
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(live, "_LIVE_WORKTREE", None)
    monkeypatch.setattr(live, "_LIVE_CHECK_AT", 0.0)
    monkeypatch.setattr(live, "_MAKE_LIVE_COMMITTED", False)
    monkeypatch.setattr(live, "_MAKE_LIVE_LOCK", asyncio.Lock())
    # This process plays the gateway: no lease client, and empty lease and start tables.
    monkeypatch.setattr(live, "_REMOVAL_LEASE_CLIENT", None)
    monkeypatch.setattr(live, "_REMOVAL_LEASES", {})
    monkeypatch.setattr(live, "_POD_STARTS", {}, raising=False)
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    monkeypatch.setattr(worktree_ops, "_GIT_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(runtime, "_POD_AVAILABLE", True)
    monkeypatch.setattr(runtime, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(runtime, "rt", rt)
    monkeypatch.setattr(repository, "MAIN_REPO", str(repo))
    monkeypatch.setattr(repository, "_repo", lambda: str(repo))
    monkeypatch.setattr(repository, "_find_worktree", find)
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value=None))
    monkeypatch.setattr(live, "_staged_target", lambda: None)
    monkeypatch.setattr(live, "_own_checkout_path", lambda: None)
    monkeypatch.setattr(live, "removal_lease", removal_lease)
    monkeypatch.setattr(live, "removal_lease_lost", lambda path: False)
    monkeypatch.setattr(live, "confirm_removal_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(repository, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(repository, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(fleet_state, "_is_pr_merged", lambda pr: False)
    monkeypatch.setattr(repository, "_git", AsyncMock(return_value="abc1234"))
    monkeypatch.setattr(fleet_state, "_fleet_forget", lambda name: None)
    monkeypatch.setattr(
        runtime, "_sel", lambda: type("_S", (), {"log_tool_invocation": lambda self, **kw: None})()
    )
    monkeypatch.setattr(runtime, "_warm_build_path", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_find_cli", lambda: ["kirocrew"])
    monkeypatch.setattr(worktree_ops, "_read_pin_strict", lambda c, n: (False, None))
    monkeypatch.setattr(worktree_ops, "_mint_pod_token_locked", lambda c, n, p: {"ok": True})

    world = SimpleNamespace(
        monkeypatch=monkeypatch,
        trees=trees,
        active=active,
        # The pod CLI stand-in awaits this before the unit goes active.
        pod_cli=AsyncMock(return_value=None),
    )

    async def run_cmd(cmd, cwd=None, env=None, timeout=None, pre_spawn=None, **kw):
        if cmd[:3] == ["kirocrew", "pod", "up"]:
            await world.pod_cli(cmd[3])
            active.add(cmd[3])
            return 0, '{"base_url": "http://127.0.0.1:1"}', ""
        if pre_spawn is not None:
            refused = await pre_spawn()
            if refused:
                return 1, "", refused
        # A real child never inherits the test process's cwd: the removal passes
        # none (git is pointed at the repo with ``-C``), so it runs under tmp_path.
        child_cwd = tmp_path if cwd is None else Path(cwd)
        assert child_cwd.resolve().is_relative_to(tmp_path.resolve()), child_cwd
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(child_cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)
    return world


async def _removal_held_at_its_git_spawn(world):
    """Start a removal of NAME and hold it at the lease renewal right before git."""
    at_gate, release = asyncio.Event(), asyncio.Event()

    async def confirm(path):
        at_gate.set()
        await asyncio.wait_for(release.wait(), BOUND)
        return True

    world.monkeypatch.setattr(live, "confirm_removal_lease", confirm)
    removal = asyncio.ensure_future(worktree_ops._worktree_remove(NAME))
    await asyncio.wait_for(at_gate.wait(), BOUND)
    return removal, release


@pytest.mark.asyncio
async def test_a_removal_issued_during_a_pod_start_does_not_delete_the_running_pods_checkout(world):
    cli_running, unit_may_start = asyncio.Event(), asyncio.Event()

    async def pod_cli(name):
        cli_running.set()
        await asyncio.wait_for(unit_may_start.wait(), BOUND)

    async def confirm(path):
        # The unit goes active while the removal is between its last pod check
        # and `git worktree remove`.
        unit_may_start.set()
        return True

    world.pod_cli = pod_cli
    world.monkeypatch.setattr(live, "confirm_removal_lease", confirm)
    pod_up = asyncio.ensure_future(worktree_ops._pod_up(NAME))
    await asyncio.wait_for(cli_running.wait(), BOUND)
    removed = await worktree_ops._worktree_remove(NAME)
    unit_may_start.set()
    started = await asyncio.wait_for(pod_up, BOUND)

    assert started["ok"] is True, started
    assert NAME in world.active
    assert removed["ok"] is False, removed
    assert world.trees[NAME].is_dir(), "the running pod's checkout was deleted"


@pytest.mark.asyncio
async def test_a_pod_start_during_a_removal_is_refused(world):
    removal, release = await _removal_held_at_its_git_spawn(world)
    started = await worktree_ops._pod_up(NAME)
    release.set()
    removed = await asyncio.wait_for(removal, BOUND)

    assert started["ok"] is False, started
    assert NAME not in world.active
    assert removed["ok"] is True, removed
    assert not world.trees[NAME].exists()


@pytest.mark.asyncio
async def test_control_a_pod_started_before_the_last_check_still_refuses_the_removal(world):
    def lost(path):
        world.active.add(NAME)
        return False

    world.monkeypatch.setattr(live, "removal_lease_lost", lost)
    removed = await worktree_ops._worktree_remove(NAME)

    assert removed == {"ok": False, "error": "pod became active again before removal — refusing"}
    assert world.trees[NAME].is_dir()


@pytest.mark.asyncio
async def test_control_a_pod_start_on_another_worktree_is_not_blocked(world):
    removal, release = await _removal_held_at_its_git_spawn(world)
    started = await asyncio.wait_for(worktree_ops._pod_up(OTHER), BOUND)
    release.set()
    removed = await asyncio.wait_for(removal, BOUND)

    assert started["ok"] is True, started
    assert world.active == {OTHER}
    assert removed["ok"] is True, removed
    assert world.trees[OTHER].is_dir()


@pytest.mark.asyncio
async def test_control_an_idle_worktree_is_removed_as_before(world):
    removed = await worktree_ops._worktree_remove(NAME)

    assert removed["ok"] is True, removed
    assert removed["stopped_pod"] is False
    assert not world.trees[NAME].exists()
    assert world.trees[OTHER].is_dir()


@pytest.mark.asyncio
async def test_a_pod_start_for_an_unknown_worktree_keeps_no_lock_row(world):
    """``_wt_lock`` rows are never removed, so a name with no worktree must not get one."""
    result = await worktree_ops._pod_up("no-such-worktree")
    assert result["ok"] is False and "unknown worktree" in result["error"]
    assert (
        "no-such-worktree" not in worktree_ops._WT_LOCKS
    ), "a pod start for a name with no worktree left a permanent lock row"


# --- two processes: the gateway runs an agent's pod start, the backend removes ---

_APP_SECRET = "test-app-secret"
_APP_TOKEN = "test-app-token"
#: Bound on a child's answers; the child imports the Dev Fleet stack first.
CHILD_BOUND = 60.0

#: The backend's side of a removal, run as its own process. It takes the removal lease
#: through the real ``GatewayPointerBroker`` that ``server.main`` installs, as
#: ``worktree_ops._worktree_remove`` does before its pod checks and its
#: ``git worktree remove``. It prints the outcome and holds a granted lease until a line
#: or EOF arrives on stdin.
_BACKEND_LEASE = """\
import asyncio
import sys

from kiro_crew.apps.builtins.dev_fleet import live, pointer_broker


async def main(port, path, secret):
    broker = pointer_broker.GatewayPointerBroker(port=port, app_secret=secret)
    live.install_removal_lease_client(
        (broker.acquire_removal_lease, broker.renew_removal_lease, broker.release_removal_lease)
    )
    try:
        async with live.removal_lease(path) as leased:
            print("granted" if leased else "refused " + str(leased.refusal), flush=True)
            if leased:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(loop.run_in_executor(None, sys.stdin.readline), 60)
    finally:
        await broker.aclose()


asyncio.run(main(int(sys.argv[1]), sys.argv[2], sys.argv[3]))
"""


@asynccontextmanager
async def _gateway(monkeypatch):
    """The gateway's real removal-lease routes on 127.0.0.1, served by this process.

    Two pieces of gateway plumbing that are not under test are stand-ins, as in
    ``test_dev_fleet_gateway_routes.py``: the app-secret token exchange, and the token
    middleware that marks a request carrying Dev Fleet's app token as that app.
    """
    monkeypatch.setattr(gateway_routes, "is_app_enabled", lambda name: name == "dev-fleet")
    monkeypatch.setattr(gateway_routes, "sel", lambda: MagicMock())

    @web.middleware
    async def stamp(request, handler):
        if request.query.get("token") == _APP_TOKEN:
            request["app"] = gateway_routes.APP_NAME
        return await handler(request)

    async def token(request):
        if request.headers.get("X-App-Secret") != _APP_SECRET:
            return web.json_response({"ok": False}, status=403)
        return web.json_response({"token": _APP_TOKEN})

    app = web.Application(middlewares=[stamp])
    app.router.add_post(f"{gateway_routes.API_PREFIX}/token", token)
    gateway_routes.register_routes(app)
    server = TestServer(app, host="127.0.0.1")
    await asyncio.wait_for(server.start_server(), BOUND)
    try:
        yield server.port
    finally:
        await asyncio.wait_for(server.close(), BOUND)


async def _backend(port, path, tmp_path):
    """Start the backend's side in its own process, working in ``tmp_path``."""
    script = tmp_path / "backend_lease.py"
    script.write_text(_BACKEND_LEASE, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(kiro_crew.__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "KIROCREW_HOME": str(tmp_path / "home"),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(port),
        path,
        _APP_SECRET,
        cwd=str(tmp_path),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _first_answer(proc) -> str:
    line = await asyncio.wait_for(proc.stdout.readline(), CHILD_BOUND)
    return line.decode().strip()


async def _release(proc) -> str:
    """Let the backend release what it holds and exit; returns the end of its stderr."""
    _out, err = await asyncio.wait_for(proc.communicate(b"release\n"), CHILD_BOUND)
    return err.decode()[-400:]


def _hold_the_pod_cli(world):
    """Make the stand-in ``pod up`` CLI wait, so the start is held mid-flight."""
    cli_running, unit_may_start = asyncio.Event(), asyncio.Event()

    async def pod_cli(name):
        cli_running.set()
        await asyncio.wait_for(unit_may_start.wait(), CHILD_BOUND)

    world.pod_cli = pod_cli
    return cli_running, unit_may_start


async def _lease_during_a_held_start(world, tmp_path, lease_name):
    """Hold an agent's start of NAME in the gateway; lease *lease_name* from another process."""
    cli_running, unit_may_start = _hold_the_pod_cli(world)
    async with _gateway(world.monkeypatch) as port:
        pod_up = asyncio.ensure_future(worktree_ops._pod_up(NAME))
        try:
            await asyncio.wait_for(cli_running.wait(), BOUND)
            backend = await _backend(port, str(world.trees[lease_name]), tmp_path)
            answer = await _first_answer(backend)
            err = await _release(backend)
        finally:
            unit_may_start.set()
            started = await asyncio.wait_for(pod_up, BOUND)
    return answer, err, started


async def _start_while_another_process_holds_the_lease(world, tmp_path, start_name):
    """Lease NAME from another process; run an agent's start of *start_name* in the gateway."""
    async with _gateway(world.monkeypatch) as port:
        backend = await _backend(port, str(world.trees[NAME]), tmp_path)
        try:
            answer = await _first_answer(backend)
            started = await asyncio.wait_for(worktree_ops._pod_up(start_name), BOUND)
        finally:
            err = await _release(backend)
    return answer, err, started


@pytest.mark.asyncio
async def test_a_backend_removal_lease_is_refused_while_the_gateway_starts_the_pod(world, tmp_path):
    answer, err, started = await _lease_during_a_held_start(world, tmp_path, NAME)

    assert started["ok"] is True, started
    assert answer == "refused busy", (
        f"another process's removal lease answered {answer!r} while the gateway was starting "
        f"the worktree's pod, so the removal would go on to delete its checkout: {err}"
    )


@pytest.mark.asyncio
async def test_a_gateway_pod_start_is_refused_while_the_backend_holds_the_removal_lease(
    world, tmp_path
):
    answer, err, started = await _start_while_another_process_holds_the_lease(world, tmp_path, NAME)

    assert answer == "granted", (answer, err)
    assert started["ok"] is False and NAME not in world.active, (
        f"the gateway started the pod while another process held its worktree's removal "
        f"lease: {started}"
    )
    assert "being removed" in started["error"], started


@pytest.mark.asyncio
async def test_control_a_lease_on_another_worktree_is_granted_during_a_gateway_pod_start(
    world, tmp_path
):
    answer, err, started = await _lease_during_a_held_start(world, tmp_path, OTHER)

    assert started["ok"] is True, started
    assert answer == "granted", (answer, err)


@pytest.mark.asyncio
async def test_control_a_gateway_pod_start_on_another_worktree_runs_during_a_backend_lease(
    world, tmp_path
):
    answer, err, started = await _start_while_another_process_holds_the_lease(
        world, tmp_path, OTHER
    )

    assert answer == "granted", (answer, err)
    assert started["ok"] is True, started
    assert world.active == {OTHER}


@pytest.mark.asyncio
async def test_control_a_lease_is_granted_once_the_gateway_pod_start_has_finished(world, tmp_path):
    async with _gateway(world.monkeypatch) as port:
        started = await asyncio.wait_for(worktree_ops._pod_up(NAME), BOUND)
        backend = await _backend(port, str(world.trees[NAME]), tmp_path)
        try:
            answer = await _first_answer(backend)
        finally:
            err = await _release(backend)

    assert started["ok"] is True, started
    assert answer == "granted", (answer, err)
    assert not getattr(live, "_POD_STARTS", {}), "a finished pod start left its reservation"
