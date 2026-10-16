"""Tests for turning a saved Coder profile into a prepared remote session."""

from __future__ import annotations

import asyncio
import functools
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import session_launch
from kiro_crew.coder.client import CoderClientError, CoderWorkspace
from kiro_crew.session_environment import SessionEnvironmentUnavailable

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
    ws = fake.workspaces[first.workspace]
    fake.workspaces[first.workspace] = CoderWorkspace(
        ws.uuid, ws.name, ws.owner, ws.template, "stopped", ""
    )
    fake.calls.clear()
    second = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    assert second.workspace == first.workspace
    assert fake.calls == [f"get {first.workspace}", f"start {first.workspace}"]


@sync
async def test_refuses_a_workspace_owned_by_someone_else(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    ws = fake.workspaces[first.workspace]
    fake.workspaces[first.workspace] = CoderWorkspace(
        ws.uuid, ws.name, "mallory", ws.template, "running", ""
    )
    with pytest.raises(SessionEnvironmentUnavailable, match="another Coder user"):
        await _envs(tmp_path, fake).prepare("sess-a", "dev")


@sync
async def test_refuses_a_name_that_now_names_another_workspace(tmp_path):
    fake = FakeCoder()
    first = await _envs(tmp_path, fake).prepare("sess-a", "dev")
    ws = fake.workspaces[first.workspace]
    fake.workspaces[first.workspace] = CoderWorkspace(
        "other-uuid", ws.name, ws.owner, ws.template, "running", ""
    )
    with pytest.raises(SessionEnvironmentUnavailable, match="names another"):
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
    path.write_text("{not json")
    with pytest.raises(SessionEnvironmentUnavailable):
        choices.get("k")


@sync
async def test_prepare_for_session_is_none_without_a_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert await session_launch.prepare_for_session("sess-a") is None
    assert await session_launch.prepare_for_session(None) is None


@sync
async def test_pool_bypass_reads_the_choice_and_fails_closed(tmp_path, monkeypatch):
    from kiro_crew.config import paths
    from kiro_crew.session_allocation_parts import get_or_create

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert await get_or_create._session_runs_remotely("k") is False
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "k", "dev"
    )
    assert await get_or_create._session_runs_remotely("k") is True
    assert await get_or_create._session_runs_remotely("other") is False
    (tmp_path / "coder" / "session-choices.json").write_text(json.dumps({"version": 9}))
    assert await get_or_create._session_runs_remotely("other") is True


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
async def test_login_runner_accepts_coder_json_larger_than_the_guest_cap(tmp_path):
    # Regression: `coder list --output json` on a deployment with a few old
    # workspaces exceeds the 64 KB guest-command cap. The lifecycle runner must
    # use the Coder client's bound instead, and still enforce one.
    import sys

    from kiro_crew.coder import remote_worker

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
    from kiro_crew.coder import client as client_mod

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


# -- dashboard endpoint -------------------------------------------------------------


async def _client(tmp_path, monkeypatch, *, owner=True):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config import paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(
        handlers,
        "_profiles",
        lambda: [{"id": "dev", "name": "Dev", "provider": "coder", "workdir": "/w"}],
    )
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: f"dashboard:{slot.key}")

    async def gate(request, operation):
        return None if owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", gate)
    import asyncio

    from kiro_crew.dashboard import chat_runner

    spawned: list[object] = []
    monkeypatch.setattr(
        chat_runner, "schedule_eager_spawn", lambda state, slot: spawned.append(slot)
    )
    slot = SimpleNamespace(key="main", _lock=asyncio.Lock(), _pending_reset_history_key=None)
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={"main": slot})
    app.router.add_get("/s/{slot}", handlers.api_chat_slot_environment_get)
    app.router.add_put("/s/{slot}", handlers.api_chat_slot_environment_put)
    app.router.add_get("/b/{slot}", handlers.api_chat_slot_environment_browse)
    app.router.add_put("/w/{slot}", handlers.api_chat_slot_environment_file_put)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.slot = slot  # type: ignore[attr-defined]
    client.spawned = spawned  # type: ignore[attr-defined]
    return client


@sync
async def test_endpoint_round_trip_and_validation(tmp_path, monkeypatch):
    client = await _client(tmp_path, monkeypatch)
    try:
        assert (await (await client.get("/s/main")).json())["profile"] is None
        r = await client.put("/s/main", json={"profile": "dev"})
        assert r.status == 200 and (await r.json())["applies"] == "next_start"
        assert (await (await client.get("/s/main")).json())["profile"] == "dev"
        assert (await client.put("/s/main", json={"profile": "nope"})).status == 400
        assert (await client.put("/s/main", json={"profile": 5})).status == 400
        assert (await client.put("/s/ghost", json={"profile": "dev"})).status == 404
        assert (await client.put("/s/main", json={"profile": None})).status == 200
        assert (await (await client.get("/s/main")).json())["profile"] is None
    finally:
        await client.close()


@sync
async def test_a_changed_choice_resets_the_idle_runtime_and_prewarms_only_local(
    tmp_path, monkeypatch
):
    client = await _client(tmp_path, monkeypatch)
    slot, spawned = client.slot, client.spawned
    try:
        # Local -> remote: the next turn must not reuse a runtime started here,
        # and nothing is prewarmed (that would start billed compute on a pick).
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        assert slot._pending_reset_history_key == "dashboard:main" and spawned == []
        # Re-picking the same environment is a no-op: no reset re-armed.
        slot._pending_reset_history_key = None
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        assert slot._pending_reset_history_key is None
        # Remote -> local: reset, and the local session may prewarm.
        assert (await client.put("/s/main", json={"profile": None})).status == 200
        assert slot._pending_reset_history_key == "dashboard:main" and spawned == [slot]
    finally:
        await client.close()


def _verified_workspace(tmp_path, session_key):
    from dataclasses import replace

    from kiro_crew.coder.registry import WorkspaceBindingRegistry

    registry = WorkspaceBindingRegistry(tmp_path / "coder" / "session-envs.json")
    binding = registry.allocate(
        session_key,
        template="micro",
        preset="",
        prefix="crewenv",
        owner_name="devuser",
        deployment_id=URL,
    )
    registry.replace(replace(binding, workspace_uuid="uuid-1", state="running"))


@sync
async def test_environment_is_fixed_once_the_session_has_a_message(tmp_path, monkeypatch):
    client = await _client(tmp_path, monkeypatch)
    slot = client.slot
    try:
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        _verified_workspace(tmp_path, "dashboard:main")
        slot.total_messages, slot.messages = 1, [{"role": "user"}]
        body = await (await client.get("/s/main")).json()
        assert body["locked"] is True and body["profile"] == "dev"
        r = await client.put("/s/main", json={"profile": None})
        assert r.status == 409 and (await r.json())["code"] == "environment_locked"
        # Re-asserting the current choice is not a change, so it is not refused.
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        assert (await (await client.get("/s/main")).json())["profile"] == "dev"
    finally:
        await client.close()


@sync
async def test_a_remote_session_whose_workspace_never_started_can_move_here(tmp_path, monkeypatch):
    """Every turn failed before compute (e.g. an unsupported backend): nothing
    lives remotely, so the session is not stuck on the remote choice."""
    client = await _client(tmp_path, monkeypatch)
    slot = client.slot
    try:
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        slot.total_messages, slot.messages = 1, [{"role": "user"}]
        assert (await (await client.get("/s/main")).json())["locked"] is False
        assert (await client.put("/s/main", json={"profile": None})).status == 200
        # Back on this machine with messages: fixed again, both ways.
        body = await (await client.get("/s/main")).json()
        assert body["profile"] is None and body["locked"] is True
        r = await client.put("/s/main", json={"profile": "dev"})
        assert r.status == 409 and (await r.json())["code"] == "environment_locked"
    finally:
        await client.close()


@sync
async def test_an_unreadable_registry_keeps_the_environment_fixed(tmp_path, monkeypatch):
    client = await _client(tmp_path, monkeypatch)
    slot = client.slot
    try:
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 200
        (tmp_path / "coder" / "session-envs.json").write_text("{not json")
        slot.total_messages, slot.messages = 1, [{"role": "user"}]
        r = await client.put("/s/main", json={"profile": None})
        assert r.status == 409 and (await r.json())["code"] == "environment_locked"
    finally:
        await client.close()


@sync
async def test_endpoint_is_owner_only(tmp_path, monkeypatch):
    client = await _client(tmp_path, monkeypatch, owner=False)
    try:
        assert (await client.get("/s/main")).status == 403
        assert (await client.put("/s/main", json={"profile": "dev"})).status == 403
        assert not (tmp_path / "coder" / "session-choices.json").exists()
    finally:
        await client.close()


# -- start progress -----------------------------------------------------------------


def test_start_progress_reports_stage_and_total_elapsed_then_clears():
    from kiro_crew import session_environment as se

    se.clear_start_stage("sess-p")
    assert se.start_progress("sess-p") is None
    se.report_start_stage("sess-p", "checking")
    se.report_start_stage("sess-p", "bogus")  # unknown stages are ignored
    se.report_start_stage("", "checking")  # keyless reports are ignored
    assert se.start_progress("sess-p")["stage"] == "checking"
    se.report_start_stage("sess-p", "provisioning")
    progress = se.start_progress("sess-p")
    assert progress["stage"] == "provisioning"
    assert progress["elapsed_s"] >= progress["stage_elapsed_s"] >= 0
    se.clear_start_stage("sess-p")
    assert se.start_progress("sess-p") is None


def test_start_progress_lists_every_stage_entered_even_between_polls():
    """A stage can finish between polls; a skipped stage is one never entered."""
    from kiro_crew import session_environment as se

    key = "sess-entered"
    se.clear_start_stage(key)
    try:
        for stage in ("checking", "connecting", "connecting", "starting_agent"):
            se.report_start_stage(key, stage)
        progress = se.start_progress(key)
        assert progress["stage"] == "starting_agent"
        # In order, once each, and provisioning absent because it never ran.
        assert progress["entered"] == ["checking", "connecting", "starting_agent"]
    finally:
        se.clear_start_stage(key)
    # A new start begins a new list.
    se.report_start_stage(key, "checking")
    try:
        assert se.start_progress(key)["entered"] == ["checking"]
    finally:
        se.clear_start_stage(key)


def test_a_start_for_a_session_already_attached_here_is_a_reconnect():
    from kiro_crew import session_environment as se

    key = "sess-reconnect"
    se.clear_start_stage(key)
    se.forget_remote_workspace(key)
    try:
        se.report_start_stage(key, "checking")
        assert se.start_progress(key)["reconnect"] is False  # first connection
        se.remember_remote_workspace(key, "Dev", "/w")  # attached mid-start
        se.report_start_stage(key, "starting_agent")
        # Decided when the start began: attaching during it does not flip it.
        assert se.start_progress(key)["reconnect"] is False
        se.clear_start_stage(key)
        # The engine restarts later in the same session: that start reconnects.
        se.report_start_stage(key, "checking")
        assert se.start_progress(key)["reconnect"] is True
    finally:
        se.clear_start_stage(key)
        se.forget_remote_workspace(key)


def test_start_progress_is_bounded():
    from kiro_crew import session_environment as se

    for i in range(se._START_PROGRESS_MAX + 10):
        se.report_start_stage(f"leak-{i}", "checking")
    try:
        assert len(se._start_progress) <= se._START_PROGRESS_MAX
    finally:
        for i in range(se._START_PROGRESS_MAX + 10):
            se.clear_start_stage(f"leak-{i}")


@sync
async def test_prepare_reports_provisioning_only_when_compute_must_start(tmp_path):
    from kiro_crew import session_environment as se

    fake = FakeCoder()
    seen: list[str] = []
    original = session_launch.report_start_stage

    def spy(key, stage):
        seen.append(stage)
        original(key, stage)

    session_launch.report_start_stage = spy  # type: ignore[assignment]
    try:
        await _envs(tmp_path, fake).prepare("sess-q", "dev")  # creates
        assert seen == ["provisioning"]
        seen.clear()
        await _envs(tmp_path, fake).prepare("sess-q", "dev")  # already running
        assert seen == []
    finally:
        session_launch.report_start_stage = original  # type: ignore[assignment]
        se.clear_start_stage("sess-q")


@sync
async def test_endpoint_reports_a_start_in_flight(tmp_path, monkeypatch):
    from kiro_crew import session_environment as se

    client = await _client(tmp_path, monkeypatch)
    try:
        assert (await (await client.get("/s/main")).json())["starting"] is None
        se.report_start_stage("dashboard:main", "provisioning")
        body = await (await client.get("/s/main")).json()
        assert body["starting"]["stage"] == "provisioning"
    finally:
        se.clear_start_stage("dashboard:main")
        await client.close()


# -- subagent inheritance -----------------------------------------------------------


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


def test_a_finished_subagent_drops_only_its_inherited_row(tmp_path):
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "choices.json")
    assert choices.forget_inherited("sub-1") is False  # no file, no lock dir
    choices.set("dashboard:root", "dev")
    choices.inherit("sub-1", "dashboard:root")
    assert choices.forget_inherited("sub-1") is True
    assert choices.resolve("sub-1") is None
    assert choices.forget_inherited("sub-1") is False
    # A session's own choice is never an inherited row.
    assert choices.forget_inherited("dashboard:root") is False
    assert choices.get("dashboard:root") == "dev"


@sync
async def test_forget_subagent_environment_is_best_effort(tmp_path, monkeypatch):
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    path = tmp_path / "coder" / "session-choices.json"
    choices = session_launch.SessionEnvironmentChoices(path)
    choices.set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    await session_launch.forget_subagent_environment("sub-9")
    assert choices.resolve("sub-9") is None
    path.write_text("{not json")
    await session_launch.forget_subagent_environment("sub-9")  # logged, not raised


@sync
async def test_prepare_waits_for_a_registry_lock_held_by_a_worker_thread(tmp_path):
    """The registry's lock is single-shot on the event loop, so on-loop calls
    would refuse a start while a sweeper or health poll briefly holds it."""
    import threading

    from kiro_crew.platform_compat import file_lock

    fake = FakeCoder()
    envs = _envs(tmp_path, fake)
    envs.registry.path.parent.mkdir(parents=True, exist_ok=True)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with open(envs.registry.lock_path, "a+b") as fd:
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
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices.set("dashboard:root", "dev")
    assert await session_launch.inherit_environment_for_subagent("dashboard:root", "sub-9")
    assert not await session_launch.inherit_environment_for_subagent("dashboard:other", "sub-10")
    seen = []

    async def fake_prepare(self, key, profile):
        seen.append((key, profile))
        return "prepared"

    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "prepare", fake_prepare)
    from kiro_crew.config import live

    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    assert await session_launch.prepare_for_session("sub-9") == "prepared"
    assert seen == [("dashboard:root", "dev")]
    assert await session_launch.prepare_for_session("sub-10") is None


# -- remote file browsing -------------------------------------------------------------


def test_browse_is_read_only_and_root_confined(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print(1)\n")
    ws = wt.Workspace(str(root))
    listing = wt.browse(ws, {"op": "list", "path": "."})
    assert listing["entries"] == [{"name": "src", "dir": True}] and listing["path"] == "."
    read = wt.browse(ws, {"op": "read", "path": "src/a.py"})
    assert read["content"] == "print(1)\n" and read["binary"] is False
    assert "outside the workspace" in wt.browse(ws, {"op": "read", "path": "../x"})["error"]
    # No write/run op exists on this entry point, whatever is asked.
    for op in ("write", "write_file", "run_command", "edit_file"):
        assert wt.browse(ws, {"op": op, "path": "src/a.py", "content": "x"})["error"]
    assert (root / "src" / "a.py").read_text() == "print(1)\n"


@sync
async def test_browse_session_runs_the_guest_browse_through_coder(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    with pytest.raises(session_browse.NotRemote):
        await session_browse.browse_session("dashboard:local", "list", ".")
    fake = FakeCoder()
    await _envs(tmp_path, fake).prepare("dashboard:remote", "dev")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:remote", "dev"
    )
    seen = {}

    async def fake_run(argv, stdin, env, **limits):
        seen.update(argv=argv, stdin=stdin, limits=limits)
        return b'{"root": "/workspace/app", "path": ".", "entries": []}'

    monkeypatch.setattr(session_browse, "run_collect", fake_run)
    out = await session_browse.browse_session("dashboard:remote", "list", "src/../x; rm -rf /")
    assert out["entries"] == []
    assert seen["argv"][:5] == ["coder", "--url", URL, "ssh", "--disable-autostart"]
    # The path rides stdin as JSON, never the command line.
    assert "rm -rf" not in " ".join(seen["argv"]) and b"rm -rf" in seen["stdin"]
    assert "--browse" in seen["argv"][-1] and "/workspace/app" in seen["argv"][-1]
    with pytest.raises(ValueError):
        await session_browse.browse_session("dashboard:remote", "write", ".")


@sync
async def test_browse_endpoint_maps_failures(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse

    client = await _client(tmp_path, monkeypatch)
    outcomes = iter(
        [
            session_browse.NotRemote("x"),
            session_browse.WorkspaceNotRunning("stopped"),
            {"error": "outside the workspace"},
            {"root": "/r", "path": ".", "entries": [{"name": "a", "dir": False}]},
        ]
    )

    async def fake_browse(key, op, path):
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(session_browse, "browse_session", fake_browse)
    try:
        assert (await (await client.get("/b/main")).json())["code"] == "not_remote"
        assert (await (await client.get("/b/main")).json())["code"] == "workspace_stopped"
        assert (await client.get("/b/main?path=..")).status == 400
        body = await (await client.get("/b/main?op=list")).json()
        assert body["entries"][0]["name"] == "a"
    finally:
        await client.close()


def test_write_request_saves_refuses_stale_and_stays_in_root(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("one\n")
    ws = wt.Workspace(str(root))
    assert wt.write_request(ws, {"path": "a.txt", "content": "two\n", "expected": "one\n"})["ok"]
    assert (root / "a.txt").read_text() == "two\n"
    stale = wt.write_request(ws, {"path": "a.txt", "content": "three\n", "expected": "one\n"})
    assert stale["conflict"] is True and (root / "a.txt").read_text() == "two\n"
    assert "outside" in wt.write_request(ws, {"path": "../x", "content": "x"})["error"]
    assert wt.write_request(ws, {"path": "new/b.txt", "content": "b"})["ok"]


def test_git_views_are_read_only_and_handle_non_repos(tmp_path):
    import shutil
    import subprocess

    from kiro_crew.remote_session import workspace_tools as wt

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


@sync
async def test_write_endpoint_maps_conflict_and_success(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse

    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **k: None)
    )
    client = await _client(tmp_path, monkeypatch)
    outcomes = iter(
        [
            {"error": "the file changed since it was opened", "conflict": True},
            {"ok": True, "path": "a.txt"},
        ]
    )

    async def fake_write(key, path, content, expected):
        return next(outcomes)

    monkeypatch.setattr(session_browse, "write_session_file", fake_write)
    try:
        body = {"path": "a.txt", "content": "x", "expected": "y"}
        r = await client.put("/w/main", json=body)
        assert r.status == 409 and (await r.json())["code"] == "file_changed"
        assert (await (await client.put("/w/main", json=body)).json())["ok"] is True
        assert (await client.put("/w/main", json={"path": 3, "content": "x"})).status == 400
    finally:
        await client.close()


@sync
async def test_remote_terminal_argv_is_coder_ssh_without_autostart(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=_config()))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    with pytest.raises(session_browse.NotRemote):
        await session_browse.remote_terminal_argv("dashboard:local")
    fake = FakeCoder()
    prepared = await _envs(tmp_path, fake).prepare("dashboard:t", "dev")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:t", "dev"
    )
    argv = await session_browse.remote_terminal_argv("dashboard:t")
    assert argv == ["coder", "--url", URL, "ssh", "--disable-autostart", prepared.workspace]


@sync
async def test_terminal_remote_slot_never_falls_back_to_a_local_shell(tmp_path, monkeypatch):
    from kiro_crew.coder import session_browse
    from kiro_crew.dashboard.handlers import terminal

    async def not_remote(key):
        raise session_browse.NotRemote(key)

    request = SimpleNamespace(
        app={"state": SimpleNamespace(_slots={"main": SimpleNamespace(key="main")})}
    )
    argv, why = await terminal._remote_terminal_argv(request, "ghost")
    assert argv is None and "Unknown chat" in why
    monkeypatch.setattr(session_browse, "remote_terminal_argv", not_remote)
    argv, why = await terminal._remote_terminal_argv(request, "main")
    assert argv is None and "runs on this machine" in why

    async def remote(key):
        return ["coder", "ssh", "--disable-autostart", "crewenv-x"]

    monkeypatch.setattr(session_browse, "remote_terminal_argv", remote)
    argv, why = await terminal._remote_terminal_argv(request, "main")
    assert argv == ["coder", "ssh", "--disable-autostart", "crewenv-x"] and why == ""


def test_the_browse_mode_has_no_metrics_op(tmp_path):
    from kiro_crew.remote_session import workspace_tools as wt

    assert wt.browse(wt.Workspace(str(tmp_path)), {"op": "stats"}) == {
        "error": "op must be list, read or git_status"
    }


@sync
async def test_unsaved_work_on_a_stopped_workspace_is_unknown_and_never_starts_it(
    tmp_path, monkeypatch
):
    from kiro_crew.coder import session_browse
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    async def fake_resolve(key):
        return session_browse._Target(SimpleNamespace(workspace="crewenv-x"), "/w", "b", tmp_path)

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
