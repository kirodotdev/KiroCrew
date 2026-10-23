"""Tests for turning a saved Coder profile into a prepared remote session."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import session_environment as se
from kiro_crew.coder import client as client_mod
from kiro_crew.coder import remote_worker, session_browse, session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.config import live, paths
from kiro_crew.platform_compat import file_lock
from kiro_crew.remote_session import workspace_tools as wt
from kiro_crew.session_allocation_parts import get_or_create
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.testing.clock import ManualClock


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


URL = "https://coder.example"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 30))

    return wrapper


def _config(**overrides):
    profile = SimpleNamespace(id="dev", name="Dev", template="micro", workdir="/workspace/app")
    values = {"coder_url": URL, "profiles": [profile]}
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCoder:
    def __init__(self, *, owner="devuser", owner_id="u-1", workspaces=None, signed_in=True):
        self.owner, self.owner_id, self.signed_in = owner, owner_id, signed_in
        self.workspaces = dict(workspaces or {})
        self.calls: list[str] = []

    async def current_user(self):
        if not self.signed_in:
            raise CoderClientError("signed out")
        return self.owner, self.owner_id

    async def get_workspace(self, name):
        self.calls.append(f"get {name}")
        return self.workspaces.get(name)

    async def create_workspace(self, *, name, template, preset, stop_after_minutes):
        self.calls.append(f"create {name} {template} {stop_after_minutes}m")
        ws = CoderWorkspace(f"uuid-{name}", name, self.owner_id, template, "running", "")
        self.workspaces[name] = ws
        return ws

    async def start_workspace(self, name):
        self.calls.append(f"start {name}")
        ws = self.workspaces[name]
        self.workspaces[name] = CoderWorkspace(ws.uuid, name, ws.owner, ws.template, "running", "")
        return self.workspaces[name]


def _envs(tmp_path, fake, config=None):
    return session_launch.CoderSessionEnvironments(
        config or _config(), tmp_path, coder_bin="coder", client_factory=lambda b, u: fake
    )


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)


def _choices(tmp_path):
    return session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")


async def _remote(tmp_path, monkeypatch, key):
    """A prepared remote ``key`` on the live config; returns the workspace name."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare(key, "dev")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
    _choices(tmp_path).set(key, "dev")
    return prepared.workspace


@sync
async def test_first_prepare_reserves_then_creates_with_autostop(tmp_path):
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert prepared.workdir == "/workspace/app"
    assert prepared.workspace.startswith("crewenv-devuser-")
    assert any(c.startswith("create ") and c.endswith(" 60m") for c in fake.calls)
    assert prepared.launch.shell.argv("true")[:6] == [
        "coder",
        "--url",
        URL,
        "ssh",
        "--disable-autostart",
        prepared.workspace,
    ]
    binding = _envs(tmp_path, fake).registry.get_by_session("sess-a")
    assert binding.workspace_uuid == f"uuid-{prepared.workspace}" and binding.state == "running"


@sync
async def test_second_prepare_reuses_and_starts_a_stopped_workspace(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    fake.workspaces[first.workspace] = replace(fake.workspaces[first.workspace], status="stopped")
    fake.calls.clear()
    second = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert second.workspace == first.workspace
    assert fake.calls == [f"get {first.workspace}", f"start {first.workspace}"]


@pytest.mark.parametrize(
    "field, value, reason",
    [("owner", "mallory", "another Coder user"), ("uuid", "other-uuid", "names another")],
)
@sync
async def test_refuses_a_workspace_whose_identity_changed(tmp_path, field, value, reason):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    fake.workspaces[first.workspace] = replace(fake.workspaces[first.workspace], **{field: value})
    with pytest.raises(SessionEnvironmentUnavailable, match=reason):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")


@sync
async def test_rebound_workspace_is_refused_before_start(tmp_path):
    for field, value, reason in [
        ("uuid", "replacement", "names another"),
        ("owner", "another-user", "another Coder user"),
        ("template", "another-template", "different template"),
    ]:
        for status in ("stopped", "failed", "canceled"):
            fake = FakeCoder()
            envs = _envs(tmp_path / field / status, fake)
            first = await envs.prepare("sess-a", "dev")
            replacement = replace(fake.workspaces[first.workspace], status=status, **{field: value})
            fake.workspaces[first.workspace] = replacement
            fake.calls.clear()
            with pytest.raises(SessionEnvironmentUnavailable, match=reason):
                await envs.prepare("sess-a", "dev")
            assert fake.calls == [f"get {first.workspace}"]
            assert fake.workspaces[first.workspace] == replacement


@sync
async def test_workspace_identity_is_checked_again_after_start(tmp_path, monkeypatch):
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    first = await envs.prepare("sess-a", "dev")
    original = fake.workspaces[first.workspace]
    fake.workspaces[first.workspace] = replace(original, status="stopped")
    start = AsyncMock(return_value=replace(original, uuid="replacement"))
    monkeypatch.setattr(fake, "start_workspace", start)
    with pytest.raises(SessionEnvironmentUnavailable, match="names another"):
        await envs.prepare("sess-a", "dev")
    start.assert_awaited_once_with(first.workspace)
    assert envs.registry.get_by_session("sess-a").workspace_uuid == original.uuid


@sync
async def test_never_recreates_a_workspace_deleted_outside_crew(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    del fake.workspaces[first.workspace]
    with pytest.raises(SessionEnvironmentUnavailable, match="deleted outside"):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")


@sync
async def test_signed_out_and_unknown_profile_and_no_url_are_clear_errors(tmp_path):
    with pytest.raises(SessionEnvironmentUnavailable, match="coder login"):
        await _envs(tmp_path, FakeCoder(signed_in=False)).prepare("s", "dev")
    with pytest.raises(SessionEnvironmentUnavailable, match="not configured"):
        await _envs(tmp_path, FakeCoder()).prepare("s", "nope")
    with pytest.raises(SessionEnvironmentUnavailable, match="Coder URL"):
        await _envs(tmp_path, FakeCoder(), _config(coder_url="")).prepare("s", "dev")


def test_choices_round_trip_owner_only_and_fail_closed(tmp_path):
    path = tmp_path / "coder" / "session-choices.json"
    choices = session_launch.SessionEnvironmentChoices(path)
    assert choices.get("k") is None
    choices.set("k", "dev")
    assert choices.get("k") == "dev"
    if hasattr(os, "getuid"):
        assert path.stat().st_mode & 0o077 == 0
    choices.set("k", None)
    assert choices.get("k") is None
    with pytest.raises(ValueError):
        choices.set("k", "bad id; rm")
    choices.set("bound", "dev")
    path.write_text("{not json")
    # Fail closed for a session that was bound; one that never was runs here.
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.get("bound")
    assert choices.get("k") is None


@sync
async def test_prepare_for_session_is_none_without_a_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _home(monkeypatch, tmp_path)
    assert await session_launch.prepare_for_session("sess-a") is None
    assert await session_launch.prepare_for_session(None) is None


@sync
async def test_pool_bypass_reads_the_choice_and_fails_closed(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    assert await get_or_create._session_runs_remotely("k") is False
    _choices(tmp_path).set("k", "dev")
    assert await get_or_create._session_runs_remotely("k") is True
    assert await get_or_create._session_runs_remotely("other") is False
    (tmp_path / "coder" / "session-choices.json").write_text(json.dumps({"version": 9}))
    assert await get_or_create._session_runs_remotely("k") is True
    assert await get_or_create._session_runs_remotely("other") is False


@sync
async def test_login_runner_drops_an_empty_token(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(self, argv, env, cwd):
        seen.update(env)
        assert cwd == tmp_path
        return b"ok"

    monkeypatch.setattr(session_launch.CoderClient, "_run", fake_run)
    await session_launch._login_runner(
        ["coder"], {"CODER_SESSION_TOKEN": "", "CODER_URL": URL}, tmp_path
    )
    assert "CODER_SESSION_TOKEN" not in seen and seen["CODER_URL"] == URL


@sync
async def test_lifecycle_and_guest_commands_authenticate_the_same_way(tmp_path, monkeypatch):
    # An inherited, expired CODER_SESSION_TOKEN must not reach `coder ssh` when the
    # lifecycle calls ignore it: both use the CLI's stored login.
    from kiro_crew.coder import remote_worker

    for name, value in (
        ("CODER_SESSION_TOKEN", "expired"),
        ("CODER_URL", "https://other.example.test"),
        ("CODER_CONFIG_DIR", "/elsewhere"),
    ):
        monkeypatch.setenv(name, value)
    seen = {}

    async def fake_run(self, argv, env, cwd):
        seen.update(env)
        return b"ok"

    monkeypatch.setattr(session_launch.CoderClient, "_run", fake_run)
    client = session_launch.CoderClient(
        "coder", URL, "", tmp_path, runner=session_launch._login_runner
    )
    await client._runner(["coder"], client._env(), tmp_path)
    guest = remote_worker._scrubbed_env()
    for name in ("CODER_SESSION_TOKEN", "CODER_CONFIG_DIR"):
        assert name not in seen and name not in guest
    assert "CODER_URL" not in guest  # the dial names the verified deployment itself
    assert guest.get("PATH") == os.environ.get("PATH")


@sync
async def test_login_runner_accepts_coder_json_larger_than_the_guest_cap(tmp_path):
    # `coder list` JSON can exceed the 64 KB guest cap; the Coder client's bound applies.
    big = 200 * 1024
    out = await session_launch._login_runner(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {big})"], {}, tmp_path
    )
    assert len(out) == big > remote_worker._OUTPUT_MAX
    with pytest.raises(remote_worker.RemoteWorkerError, match="too much data"):
        await remote_worker.run_collect([sys.executable, "-c", f"print('x' * {big})"], b"", None)


@pytest.mark.parametrize("returncode", [0, 7])
@sync
async def test_login_runner_discards_stderr_and_bounds_stdout(
    tmp_path, monkeypatch, caplog, returncode
):
    secret = b"unlabelled-credential-canary"
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(b"x" * 1024)
    stdout.feed_eof()
    stderr.feed_data(secret * 1024)
    stderr.feed_eof()
    process = SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        wait=AsyncMock(return_value=returncode),
        returncode=returncode,
        communicate=AsyncMock(return_value=(b"x" * 1024, secret * 1024)),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(client_mod, "create_subprocess_limited", spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_mod, "_OUTPUT_MAX_BYTES", 64)
    drains = []
    original_drain = session_launch.CoderClient._drain_bounded

    async def drain(stream, limit):
        result = await original_drain(stream, limit)
        drains.append((stream, limit, result))
        return result

    monkeypatch.setattr(session_launch.CoderClient, "_drain_bounded", staticmethod(drain))
    reason = r"failed \(exit 7\)" if returncode else "returned too much data"
    with pytest.raises(CoderClientError, match=reason) as exc:
        await session_launch._login_runner(["coder", "list"], {}, tmp_path)
    assert secret.decode() not in str(exc.value) + caplog.text
    assert exc.value.__cause__ is None
    assert (stderr, 0, (b"", True)) in drains
    assert (stdout, 64, (b"x" * 64, True)) in drains
    assert stderr.at_eof() and stdout.at_eof()
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)


def test_a_live_parents_placement_outlasts_the_runtimes_that_end_around_it(monkeypatch):
    # Admission reads a parent's placement from this bounded table; each entry
    # leaves when its own runtime ends, so ended runtimes never evict a live one.
    monkeypatch.setattr(se, "_remote_workspaces", {})
    monkeypatch.setattr(se, "_remote_workspace_owners", {})
    parent, replacement = object(), object()
    se.remember_remote_workspace("dash:parent", "Dev", "/w", owner=parent)
    for n in range(se._REMOTE_WORKSPACES_MAX + 10):
        runtime = object()
        se.remember_remote_workspace(f"dash:other-{n}", "Dev", "/w", owner=runtime)
        se.forget_remote_workspace(f"dash:other-{n}", owner=runtime)
    assert se.remote_workspace_for("dash:parent") == ("Dev", "/w")
    # A replacement runtime took the entry: the old one's end leaves it alone.
    se.remember_remote_workspace("dash:parent", "Dev", "/w", owner=replacement)
    se.forget_remote_workspace("dash:parent", owner=parent)
    assert se.remote_workspace_for("dash:parent") == ("Dev", "/w")
    se.forget_remote_workspace("dash:parent", owner=replacement)
    assert se.remote_workspace_for("dash:parent") is None


def test_subagent_inherits_the_root_owner_and_a_local_parent_writes_nothing(tmp_path):
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    assert choices.inherit("sub-1", "dashboard:local") is None
    assert not (tmp_path / "choices.json").exists()
    choices.set("dashboard:root", "dev")
    assert choices.inherit("sub-1", "dashboard:root") == "dev"
    assert choices.resolve("sub-1") == ("dev", "dashboard:root")
    # A subagent of a subagent lands in the ROOT session's workspace.
    assert choices.inherit("sub-2", "sub-1") == "dev"
    assert choices.resolve("sub-2") == ("dev", "dashboard:root")
    assert choices.get("sub-2") == "dev"
    with pytest.raises(ValueError):
        choices.inherit("dashboard:root", "dashboard:root")


def test_a_malformed_inheritance_row_fails_closed(tmp_path):
    path = tmp_path / "choices.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {"sub": {"provider": "coder", "configuration": "dev", "parent": 7}},
            }
        )
    )
    with pytest.raises(SessionEnvironmentUnavailable):
        session_launch.SessionEnvironmentChoices(path).resolve("sub")


@pytest.mark.parametrize(
    "row",
    [
        {"provider": "coder", "configuration": None},
        {"provider": "coder"},
        {"provider": "coder", "configuration": "../x"},
        {"provider": "other", "configuration": "dev"},
        "dev",
    ],
)
def test_a_malformed_row_refuses_rather_than_running_locally(tmp_path, row):
    # Only a remote choice writes a row, so one this build cannot read must not
    # silently hand a bound session the gateway's own tools.
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    choices.set("dashboard:a", "dev")
    raw = json.loads(choices.path.read_text())
    raw["sessions"]["dashboard:a"] = row
    choices.path.write_text(json.dumps(raw))
    with pytest.raises(SessionEnvironmentUnavailable, match="invalid"):
        choices.resolve("dashboard:a")
    assert choices.resolve("dashboard:other") is None


def test_a_nested_continuation_keeps_the_root_workspace_after_its_parent_ends(tmp_path):
    """Root -> A -> B; A and B finish; B's continuation re-inherits from A."""
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    choices.set("dashboard:root", "dev")
    assert choices.inherit("subagent:a", "dashboard:root") == "dev"
    assert choices.inherit("subagent:b", "subagent:a") == "dev"
    choices.forget_inherited("subagent:b")
    choices.forget_inherited("subagent:a")
    assert choices.inherit("subagent:b", "subagent:a") == "dev"
    assert choices.resolve("subagent:b") == ("dev", "dashboard:root")


def test_a_finished_subagent_ends_only_its_inherited_row_and_ttl_prunes_it(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, session_launch)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    assert choices.forget_inherited("sub-1") is False  # no file, no lock dir
    choices.set("dashboard:root", "dev")
    choices.inherit("sub-1", "dashboard:root")
    assert choices.forget_inherited("sub-1") is True
    assert choices.resolve("sub-1") == ("dev", "dashboard:root")  # kept for continuations
    clock.advance(session_launch.ENDED_ROW_TTL_SECS + 1)
    choices.inherit("sub-2", "dashboard:root")  # any write prunes aged-out rows
    assert choices.resolve("sub-1") is None and choices.resolve("sub-2") is not None
    # A session's own choice is never an inherited row.
    assert choices.forget_inherited("dashboard:root") is False
    assert choices.get("dashboard:root") == "dev"


@sync
async def test_forget_subagent_environment_is_best_effort(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    choices = _choices(tmp_path)
    choices.set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    await session_launch.forget_subagent_environment("sub-9")
    assert "ended" in json.loads(choices.path.read_text())["sessions"]["sub-9"]
    choices.path.write_text("{not json")
    await session_launch.forget_subagent_environment("sub-9")  # logged, not raised


@sync
async def test_prepare_waits_for_a_registry_lock_held_by_a_worker_thread(tmp_path):
    """On-loop calls would refuse while a sweeper thread briefly holds the lock."""
    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    envs.registry.path.parent.mkdir(parents=True, exist_ok=True)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with envs.registry._open_lock() as fd:
            with file_lock(fd.fileno(), exclusive=True, required=True):
                held.set()
                release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        asyncio.get_running_loop().call_later(0.3, release.set)
        prepared = await envs.prepare("sess-a", "dev")
    finally:
        release.set()
        holder.join(10)
    assert envs.registry.get_by_session("sess-a").workspace_uuid == f"uuid-{prepared.workspace}"


@sync
async def test_an_inherited_session_prepares_its_owners_workspace(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    _choices(tmp_path).set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    assert not await session_launch.inherit_environment_for_subagent("dashboard:other", "sub-10")
    seen = []

    async def fake_prepare(self, key, profile):
        seen.append((key, profile))
        return "prepared"

    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", fake_prepare)

    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    assert await session_launch.prepare_for_session("sub-9") == "prepared"
    assert seen == [("dashboard:root", "dev")]
    assert await session_launch.prepare_for_session("sub-10") is None


def test_browse_is_read_only(tmp_path):
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_bytes(b"print(1)\n")
    ws = wt.Workspace(str(root))
    # No write/run op exists on this entry point, whatever is asked.
    for op in ("write", "write_file", "run_command", "edit_file", "list", "read"):
        assert wt.browse(ws, {"op": op, "path": "src/a.py", "content": "x"})["error"]
    assert (root / "src" / "a.py").read_text() == "print(1)\n"


def test_the_guest_has_no_write_mode(tmp_path):
    with pytest.raises(SystemExit):
        wt.main(["--root", str(tmp_path), "--write"])


def test_git_views_are_read_only_and_handle_non_repos(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ws = wt.Workspace(str(root))
    assert wt.browse(ws, {"op": "git_status"})["git"] is False
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    (root / "f.txt").write_text("x\n")
    status = wt.browse(ws, {"op": "git_status"})
    assert status["git"] is True and "?? f.txt" in status["output"]


def test_the_browse_mode_has_no_metrics_op(tmp_path):
    assert wt.browse(wt.Workspace(str(tmp_path)), {"op": "stats"}) == {
        "error": "op must be git_status"
    }


@sync
async def test_unsaved_work_on_a_stopped_workspace_is_unknown_and_never_starts_it(
    tmp_path, monkeypatch
):
    async def fake_resolve(key):
        return session_browse._Target(SimpleNamespace(workspace="crewenv-x"), "/w", "b")

    requests = []

    async def stopped(target, mode, request):
        requests.append((mode, request))
        raise session_browse.WorkspaceNotRunning("stopped")

    monkeypatch.setattr(session_browse, "_resolve", fake_resolve)
    monkeypatch.setattr(session_browse, "_ask", stopped)
    assert await session_browse.unsaved_work("k") is None
    # One read-only git view, nothing that could start compute.
    assert requests == [("browse", {"op": "git_status", "path": "."})]

    async def unreachable(target, mode, request):
        raise SessionEnvironmentUnavailable("the remote workspace did not answer")

    monkeypatch.setattr(session_browse, "_ask", unreachable)
    assert await session_browse.unsaved_work("k") is None

    async def running(target, mode, request):
        return {"git": True, "output": "## main...origin/main [ahead 2]\n M a.py\n"}

    monkeypatch.setattr(session_browse, "_ask", running)
    assert await session_browse.unsaved_work("k") == "1 uncommitted file, 2 unpushed commits"
