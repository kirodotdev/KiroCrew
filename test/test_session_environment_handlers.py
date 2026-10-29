"""Dashboard endpoints for Session Environments: the slot environment, browse,
file save, readiness check and workspaces routes, and the remote terminal."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_coder_session_launch import URL, FakeCoder, _config, _home, sync

from kiro_crew import session_environment as se
from kiro_crew.coder import session_browse, session_launch
from kiro_crew.coder.client import CoderClient, CoderClientError
from kiro_crew.coder.registry import WorkspaceBindingRegistry
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers import session_environments as handlers
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


def _scripted(*outcomes):
    """An async stand-in answering each call with the next outcome, raising exceptions."""
    queue = iter(outcomes)

    async def answer(*_args, **_kwargs):
        value = next(queue)
        if isinstance(value, Exception):
            raise value
        return value

    return answer


@contextlib.asynccontextmanager
async def _client(tmp_path, monkeypatch, *, owner=True):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        handlers,
        "_profiles",
        lambda: [{"id": "dev", "name": "Dev", "provider": "coder", "workdir": "/w"}],
    )
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: f"dashboard:{slot.key}")

    async def gate(request, operation):
        return None if owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(handlers, "require_owner_dashboard_request", gate)

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
    app.router.add_post("/check", handlers.api_session_environments_check)
    app.router.add_get("/ws", handlers.api_session_environments_workspaces_get)
    app.router.add_get("/templates", handlers.api_session_environments_templates_get)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.slot = slot  # type: ignore[attr-defined]
    client.spawned = spawned  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        await client.close()


async def _env_of(client):
    return await (await client.get("/s/main")).json()


def _choose(client, profile, slot="main"):
    return client.put(f"/s/{slot}", json={"profile": profile})


async def _refused_as_locked(client, profile):
    r = await _choose(client, profile)
    return r.status == 409 and (await r.json())["code"] == "environment_locked"


def _has_a_message(slot):
    slot.total_messages, slot.messages = 1, [{"role": "user"}]


@sync
async def test_endpoint_round_trip_and_validation(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        assert (await _env_of(client))["profile"] is None
        r = await _choose(client, "dev")
        assert r.status == 200 and (await r.json())["applies"] == "next_start"
        assert (await _env_of(client))["profile"] == "dev"
        assert (await _choose(client, "nope")).status == 400
        assert (await _choose(client, 5)).status == 400
        assert (await _choose(client, "dev", "ghost")).status == 404
        assert (await _choose(client, None)).status == 200
        assert (await _env_of(client))["profile"] is None


@sync
async def test_a_changed_choice_resets_the_idle_runtime_and_prewarms_only_local(
    tmp_path, monkeypatch
):
    async with _client(tmp_path, monkeypatch) as client:
        slot, spawned = client.slot, client.spawned
        # Local -> remote prewarms nothing (that would start billed compute) and arms
        # no reset: the switch removed any runtime itself, and an armed reset would
        # tear the remote runtime down after its first turn.
        slot._pending_reset_history_key = "dashboard:main"  # owed by an earlier switch
        assert (await _choose(client, "dev")).status == 200
        assert slot._pending_reset_history_key is None and spawned == []
        # Re-picking the same environment re-arms no reset.
        assert (await _choose(client, "dev")).status == 200
        assert slot._pending_reset_history_key is None
        # Remote -> local resets and may prewarm.
        assert (await _choose(client, None)).status == 200
        assert slot._pending_reset_history_key == "dashboard:main" and spawned == [slot]


@sync
async def test_switching_to_remote_drops_a_local_prewarm(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        prewarm = asyncio.ensure_future(asyncio.Event().wait())
        client.slot._eager_spawn_task = prewarm
        removed: list[str] = []
        sessions = SimpleNamespace(
            has_session=lambda key: True, remove=AsyncMock(side_effect=removed.append)
        )
        client.server.app["state"].sessions = sessions
        assert (await _choose(client, "dev")).status == 200
        assert prewarm.cancelled() and removed == ["dashboard:main"]


@sync
async def test_binding_a_chat_retires_its_open_local_shells(tmp_path, monkeypatch):
    """A connected local shell is never re-checked per keystroke, so binding its
    chat closes it with the wrong-machine code instead of letting a tab with a
    stale placement read keep typing into the gateway."""
    killed: list[object] = []
    closed: list[tuple[object, str | None]] = []

    async def kill(sess):
        killed.append(sess)

    async def close(ws, *, error_message=None, error_code=None, close_code=None):
        closed.append((ws, error_code))

    monkeypatch.setattr(terminal, "_kill_session", kill)
    monkeypatch.setattr(terminal, "_close_terminal_ws_bounded", close)

    def shell(chat_slot, remote=False):
        return SimpleNamespace(chat_slot=chat_slot, remote=remote, ws=SimpleNamespace(closed=False))

    mine, other, workspace = shell("main"), shell("other"), shell("main", remote=True)
    async with _client(tmp_path, monkeypatch) as client:
        registry = {"a": mine, "b": other, "c": workspace, "d": None}
        client.server.app["state"]._terminal_sessions = registry
        # Back to this machine retires nothing.
        assert (await _choose(client, None)).status == 200
        assert killed == [] and set(registry) == {"a", "b", "c", "d"}
        assert (await _choose(client, "dev")).status == 200
        assert killed == [mine] and closed == [(mine.ws, "terminal_wrong_machine")]
        assert set(registry) == {"b", "c", "d"}


def _verified_workspace(tmp_path, session_key):
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
async def test_a_restored_chat_loads_its_window_off_the_loop_before_the_lock_check(
    tmp_path, monkeypatch
):
    """A restored sidebar row has not read its transcript yet: reading
    ``messages`` there would replay it on the event loop."""
    import threading

    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        loop_thread = threading.current_thread()
        reads: list[bool] = []

        def read(_slot):
            reads.append(threading.current_thread() is not loop_thread)
            return ([{"role": "user"}],)

        def load_window(rows):
            slot.window_pending = False
            slot.messages = rows

        assert (await _choose(client, "dev")).status == 200
        slot.window_pending = True
        slot._lazy_window = SimpleNamespace(read=read)
        slot.load_window = load_window
        body = await _env_of(client)
        assert reads == [True] and body["locked"] is False  # remote, never created
        # A transcript that cannot be read is not a free switch.
        slot.window_pending = True
        slot._lazy_window = SimpleNamespace(read=lambda _s: (_ for _ in ()).throw(OSError("gone")))
        assert (await _env_of(client))["locked"] is True


@sync
async def test_environment_is_fixed_once_the_session_has_a_message(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        _verified_workspace(tmp_path, "dashboard:main")
        _has_a_message(slot)
        body = await _env_of(client)
        assert body["locked"] is True and body["profile"] == "dev"
        assert await _refused_as_locked(client, None)
        # Re-asserting the current choice is not a change, so not refused.
        assert (await _choose(client, "dev")).status == 200
        assert (await _env_of(client))["profile"] == "dev"


@sync
async def test_a_remote_session_whose_workspace_never_started_can_move_here(tmp_path, monkeypatch):
    """Every turn failed before compute, so the session is not stuck remote."""
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        _has_a_message(slot)
        assert (await _env_of(client))["locked"] is False
        assert (await _choose(client, None)).status == 200
        # Back on this machine with messages: fixed again, both ways.
        body = await _env_of(client)
        assert body["profile"] is None and body["locked"] is True
        assert await _refused_as_locked(client, "dev")


@sync
async def test_a_switch_waits_for_a_first_start_in_flight(tmp_path, monkeypatch):
    """A cold start holds the session switch lock while it creates the workspace;
    a switch queued behind it reads the created workspace and is refused."""
    from kiro_crew.llm_helpers_parts.model_fallback import slot_switch_session_lock
    from kiro_crew.testing.wait import until_parked

    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        _has_a_message(slot)
        start = slot_switch_session_lock("dashboard:main")
        await start.acquire()
        try:
            switch = asyncio.ensure_future(_refused_as_locked(client, None))
            # The switch is parked on the start's lock, not finished.
            await until_parked(start)
            assert not switch.done()
            _verified_workspace(tmp_path, "dashboard:main")
        finally:
            start.release()
        assert await switch
        assert (await _env_of(client))["profile"] == "dev"


@sync
async def test_an_unreadable_registry_keeps_the_environment_fixed(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        slot = client.slot
        assert (await _choose(client, "dev")).status == 200
        (tmp_path / "coder" / "session-envs.json").write_text("{not json")
        _has_a_message(slot)
        assert await _refused_as_locked(client, None)


@sync
async def test_endpoint_is_owner_only(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch, owner=False) as client:
        assert (await client.get("/s/main")).status == 403
        assert (await _choose(client, "dev")).status == 403
        assert not (tmp_path / "coder" / "session-choices.json").exists()


@sync
async def test_endpoint_reports_a_start_in_flight(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        try:
            assert (await _env_of(client))["starting"] is None
            se.report_start_stage("dashboard:main", "provisioning")
            body = await _env_of(client)
            assert body["starting"]["stage"] == "provisioning"
        finally:
            se.clear_start_stage("dashboard:main")


@sync
async def test_browse_endpoint_maps_failures(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        fake_browse = _scripted(
            session_browse.NotRemote("x"),
            session_browse.WorkspaceNotRunning("stopped"),
            {"error": "outside the workspace"},
            {"root": "/r", "path": ".", "entries": [{"name": "a", "dir": False}]},
        )
        monkeypatch.setattr(session_browse, "browse_session", fake_browse)
        assert (await (await client.get("/b/main")).json())["code"] == "not_remote"
        assert (await (await client.get("/b/main")).json())["code"] == "workspace_stopped"
        assert (await client.get("/b/main?path=..")).status == 400
        body = await (await client.get("/b/main?op=list")).json()
        assert body["entries"][0]["name"] == "a"


@sync
async def test_a_save_made_under_an_old_working_folder_is_refused(tmp_path, monkeypatch):
    """The route hands the folder to the writer and maps its refusal; the check
    itself runs against the writer's own resolved target (test_coder_session_browse)."""
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **k: None)
    )
    async with _client(tmp_path, monkeypatch) as client:
        seen: list[object] = []

        async def fake_write(key, path, content, expected, *, workdir=None):
            seen.append(workdir)
            if workdir is not None and workdir != "/workspace/b":
                raise session_browse.WorkdirChanged(key)
            return {"ok": True, "path": path}

        monkeypatch.setattr(session_browse, "write_session_file", fake_write)
        body = {"path": "README.md", "content": "x", "expected": "y"}
        r = await client.put("/w/main", json={**body, "workdir": "/workspace/a"})
        assert r.status == 409 and (await r.json())["code"] == "root_changed"
        assert (await client.put("/w/main", json={**body, "workdir": 5})).status == 400
        assert (await client.put("/w/main", json={**body, "workdir": "/workspace/b"})).status == 200
        # An older client sends no workdir and keeps today's behaviour.
        assert (await client.put("/w/main", json=body)).status == 200
        assert seen == ["/workspace/a", "/workspace/b", None]


@sync
async def test_write_endpoint_maps_conflict_and_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **k: None)
    )
    async with _client(tmp_path, monkeypatch) as client:
        fake_write = _scripted(
            {"error": "the file changed since it was opened", "conflict": True},
            {"ok": True, "path": "a.txt"},
            ValueError("bad path"),
            session_browse.NotRemote("x"),
            session_browse.WorkspaceNotRunning("stopped"),
            SessionEnvironmentUnavailable("down"),
            {"error": "outside the workspace"},
        )
        monkeypatch.setattr(session_browse, "write_session_file", fake_write)
        body = {"path": "a.txt", "content": "x", "expected": "y"}
        r = await client.put("/w/main", json=body)
        assert r.status == 409 and (await r.json())["code"] == "file_changed"
        assert (await (await client.put("/w/main", json=body)).json())["ok"] is True
        assert (await client.put("/w/main", json={"path": 3, "content": "x"})).status == 400
        assert (await client.put("/w/main", json={**body, "expected": 5})).status == 400
        mapped = [(400, "invalid_request"), (409, "not_remote"), (409, "workspace_stopped")]
        mapped += [(502, "unavailable"), (400, "path_refused")]
        for status, code in mapped:
            r = await client.put("/w/main", json=body)
            assert (r.status, (await r.json())["code"]) == (status, code)


class ProbingCoder(FakeCoder):
    async def probe(self, *, template, preset):
        if template != "micro":
            raise CoderClientError(f"no template {template}")


@sync
async def test_check_endpoint_reports_sign_in_and_each_profile(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        fake = ProbingCoder(signed_in=False)
        gpu = SimpleNamespace(id="gpu", name="GPU", template="gpu", workdir="/w")
        configs = iter(
            [_config(coder_url=""), _config(), _config(profiles=[_config().profiles[0], gpu])]
        )
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=next(configs))
        )
        monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
        body = await (await client.post("/check")).json()
        assert body == {"coder": {"ok": False, "error": "no Coder URL is saved"}, "profiles": []}
        body = await (await client.post("/check")).json()
        # Coder's own words, not a guess that it is a sign-in problem (a timeout
        # or a bad identity reply needs a different fix); the card adds the
        # coder login hint itself.
        assert body == {"coder": {"ok": False, "error": "signed out"}, "profiles": []}
        fake.signed_in = True
        body = await (await client.post("/check")).json()
        assert body["coder"] == {"ok": True, "owner": "devuser"}
        assert body["profiles"] == [
            {"id": "dev", "ok": True},
            {"id": "gpu", "ok": False, "error": "no template gpu"},
        ]


@sync
async def test_check_endpoint_reports_a_missing_coder_cli_instead_of_failing(tmp_path, monkeypatch):
    def missing(self):
        raise SessionEnvironmentUnavailable("the coder CLI is not installed on this machine")

    async with _client(tmp_path, monkeypatch) as client:
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=_config())
        )
        monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", missing)
        resp = await client.post("/check")
        assert resp.status == 200
        body = await resp.json()
        assert body["coder"]["ok"] is False and "not installed" in body["coder"]["error"]
        assert body["profiles"] == []


class TemplateListingCoder:
    """Answers ``coder templates list`` with both record shapes Coder has used."""

    _json = staticmethod(CoderClient._json)

    def __init__(self):
        self.fail = False

    async def _call(self, *args):
        assert args == ("templates", "list", "--output", "json")
        if self.fail:
            raise CoderClientError("coder: not logged in")
        return json.dumps(
            [{"Template": {"name": "python-dev"}}, {"name": "gpu"}, {"Template": {}}, "junk"]
        ).encode()


@sync
async def test_templates_endpoint_lists_the_deployment_templates(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        fake = TemplateListingCoder()
        configs = iter([_config(coder_url=""), _config(), _config()])
        monkeypatch.setattr(
            KiroCrewConfig, "load", lambda: SimpleNamespace(session_environments=next(configs))
        )
        monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: fake)
        body = await (await client.get("/templates")).json()
        assert body == {"templates": [], "error": "no Coder URL is saved"}
        assert await (await client.get("/templates")).json() == {"templates": ["gpu", "python-dev"]}
        fake.fail = True
        body = await (await client.get("/templates")).json()
        assert body == {"templates": [], "error": "coder: not logged in"}


@sync
async def test_workspaces_endpoint_lists_owned_rows_and_reports_an_unreadable_registry(
    tmp_path, monkeypatch
):
    async with _client(tmp_path, monkeypatch) as client:
        _verified_workspace(tmp_path, "dashboard:chat-7")
        _verified_workspace(tmp_path, "slack:T1")
        handlers._choices().set("dashboard:chat-7", "dev")
        rows = (await (await client.get("/ws")).json())["workspaces"]
        assert sorted(r["slot"] for r in rows) == ["", "chat-7"]
        # Each row names its chat's profile, so the card can warn before a removal.
        assert {r["slot"]: r["profile"] for r in rows} == {"chat-7": "dev", "": ""}
        assert {r["state"] for r in rows} == {"running"} and rows[0]["template"] == "micro"
        assert all(r["workspace"].startswith("crewenv-") for r in rows)
        # A registry that cannot be read is not an empty inventory: the card shows
        # its error notice instead of "no workspaces yet".
        (tmp_path / "coder" / "session-envs.json").write_text("{not json")
        resp = await client.get("/ws")
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "workspaces_unreadable" and "workspaces" not in body
        assert str(tmp_path) not in body["error"]

        def io_failure(self):
            raise OSError(5, "I/O error", str(tmp_path / "coder" / "session-envs.json"))

        monkeypatch.setattr(WorkspaceBindingRegistry, "list_bindings", io_failure)
        resp = await client.get("/ws")
        assert resp.status == 500 and (await resp.json())["code"] == "workspaces_unreadable"
        assert str(tmp_path) not in (await resp.json())["error"]


@sync
async def test_terminal_remote_slot_never_falls_back_to_a_local_shell(tmp_path, monkeypatch):
    async def not_remote(key):
        raise session_browse.NotRemote(key)

    request = SimpleNamespace(
        app={"state": SimpleNamespace(_slots={"main": SimpleNamespace(key="main")})}
    )
    argv, why, code = await terminal._remote_terminal_argv(request, "ghost")
    # A chat not restored yet after a restart: no shell, but the tab redials.
    assert argv is None and "Unknown chat" in why and code == "terminal_placement_unavailable"
    monkeypatch.setattr(session_browse, "remote_terminal_argv", not_remote)
    argv, why, code = await terminal._remote_terminal_argv(request, "main")
    assert argv is None and "runs on this machine" in why and code == "terminal_wrong_machine"

    async def coder_down(key):
        raise se.SessionEnvironmentUnavailable("coder whoami failed")

    # A placement that could not be read is not "wrong machine": the tab redials.
    monkeypatch.setattr(session_browse, "remote_terminal_argv", coder_down)
    argv, why, code = await terminal._remote_terminal_argv(request, "main")
    assert argv is None and "not available" in why and code == "terminal_placement_unavailable"

    async def remote(key):
        return ["coder", "ssh", "--disable-autostart", "crewenv-x"]

    monkeypatch.setattr(session_browse, "remote_terminal_argv", remote)
    argv, why, code = await terminal._remote_terminal_argv(request, "main")
    assert argv == ["coder", "ssh", "--disable-autostart", "crewenv-x"] and why == "" and code == ""


@sync
async def test_the_title_poller_never_reports_gateway_paths_for_a_workspace_shell(monkeypatch):
    """The workspace shell's local process is the provider CLI: its cwd and title
    are this gateway's, so no frame names them."""
    from unittest.mock import MagicMock

    from terminal_handler_helpers import _make_session

    from kiro_crew.testing.wait import async_wait_until

    ws = AsyncMock()
    ws.closed = False
    sess = _make_session(session_id="s1", ws=ws)
    sess.remote = True
    title = MagicMock(return_value="ssh")
    cwd = MagicMock(return_value="/srv/gateway-project")
    monkeypatch.setattr(terminal, "_session_title", title)
    monkeypatch.setattr(terminal, "_session_cwd", cwd)
    app = {"state": SimpleNamespace(_terminal_sessions={"s1": sess})}
    poller = asyncio.ensure_future(terminal.poll_terminal_titles(app))
    try:
        # The tick clears the flag before it would probe; a second clear means
        # the first tick ran to its end.
        for _ in range(2):
            sess.frames_dirty = True
            await async_wait_until(lambda: not sess.frames_dirty, timeout=10)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
    title.assert_not_called()
    cwd.assert_not_called()
    ws.send_str.assert_not_awaited()


def test_a_workspace_shell_gets_the_scrubbed_coder_environment(monkeypatch):
    """The workspace shell's local ``coder ssh`` signs in with the stored login that
    verified the workspace, never an inherited ``CODER_*`` override."""
    monkeypatch.setenv("CODER_SESSION_TOKEN", "inherited")
    monkeypatch.setenv("CODER_URL", "https://other.example")
    monkeypatch.setenv("CODER_CONFIG_DIR", "/elsewhere")
    monkeypatch.setenv("HOME_MARKER_FOR_TEST", "kept")
    env = terminal._remote_pty_env({"TERM": "xterm-256color", "KIROCREW_TERMINAL": "1"})
    assert not any(name.startswith("CODER_") for name in env)
    assert env["TERM"] == "xterm-256color" and env["KIROCREW_TERMINAL"] == "1"
    assert env["HOME_MARKER_FOR_TEST"] == "kept"


def _unreadable_choices(tmp_path):
    (tmp_path / "coder").mkdir(parents=True, exist_ok=True)
    (tmp_path / "coder" / "session-choices.json").write_text("{not json")


@sync
async def test_flag_off_placement_read_skips_an_unreadable_choices_store(tmp_path, monkeypatch):
    async with _client(tmp_path, monkeypatch) as client:
        assert (await _choose(client, "dev")).status == 200
        _unreadable_choices(tmp_path)
        r = await client.get("/s/main")
        assert r.status == 500 and (await r.json())["code"] == "choices_unreadable"
        # Flag off: every chat runs here, so the store is never read and a local
        # chat's Files and Terminal are not locked by it.
        monkeypatch.delenv("KIROCREW_DEV_MODE")
        r = await client.get("/s/main")
        assert r.status == 200 and (await r.json())["profile"] is None


@sync
async def test_a_local_terminal_for_a_remote_chat_is_refused_without_remote_slot(
    tmp_path, monkeypatch
):
    _home(monkeypatch, tmp_path)
    from kiro_crew.dashboard import chat_utils

    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    request = SimpleNamespace(
        app={"state": SimpleNamespace(_slots={"main": SimpleNamespace(key="main")})}
    )
    # Unbound chat: a local shell is fine.
    assert await terminal._local_terminal_refusal(request, "main") == ("", "")
    # A chat not restored yet (a restart is still loading slots): its placement is
    # unknown, so no local shell, and a code the tab redials on.
    why, code = await terminal._local_terminal_refusal(request, "ghost")
    assert "not loaded yet" in why and code == "terminal_placement_unavailable"
    # Bound to a remote profile: the gateway refuses the local shell itself.
    se._choices().set("dashboard:main", "dev")
    why, code = await terminal._local_terminal_refusal(request, "main")
    assert "remote environment" in why and code == "terminal_wrong_machine"
    # An unreadable store fails closed while the flag is on, with a code the tab
    # redials on rather than one that parks it...
    _unreadable_choices(tmp_path)
    why, code = await terminal._local_terminal_refusal(request, "main")
    assert "Could not read" in why and code == "terminal_placement_unavailable"
    # ...and is never read while it is off.
    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert await terminal._local_terminal_refusal(request, "main") == ("", "")


@sync
async def test_terminal_socket_refuses_a_local_shell_for_a_remote_chat(tmp_path, monkeypatch):
    from terminal_handler_helpers import _make_app

    from kiro_crew.dashboard import chat_utils

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    se._choices().set("dashboard:main", "dev")
    app = _make_app()
    app["state"]._slots = {"main": SimpleNamespace(key="main")}
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # No remote_slot: the client did not ask for the workspace shell, and the
        # gateway still refuses to open the chat's terminal on this machine. The
        # refusal is a coded frame, since a browser cannot read a 409 body.
        ws = await client.ws_connect(
            "/api/ws/terminal/abc123?slot=main&cwd=/tmp",
            headers={"Origin": "http://localhost:5476"},
        )
        frame = await ws.receive_json(timeout=5)
        assert frame["type"] == "error" and frame["code"] == "terminal_wrong_machine"
        assert "remote environment" in frame["message"]
        await ws.receive(timeout=5)
        assert ws.closed
        assert "abc123" not in app["state"]._terminal_sessions
    finally:
        await client.close()


async def _close_with_shells(client, app) -> None:
    """Tear down every shell the test left published, then the client. A failed
    assertion must not leave a real PTY and its blocking reader holding the worker."""
    registry = app["state"]._terminal_sessions
    try:
        for sid in list(registry):
            sess = registry.pop(sid, None)
            if sess is not None:
                await terminal._kill_session(sess)
    finally:
        await client.close()


async def _error_code_of(ws, *, timeout: float = 30.0) -> str:
    """The code of the next error frame on ``ws``. Shell output (BINARY frames)
    and other control frames may arrive first and are skipped; a close before
    the error, or no error within ``timeout``, fails."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        assert remaining > 0, "no error frame arrived in time"
        msg = await ws.receive(timeout=remaining)
        if msg.type == web.WSMsgType.BINARY:
            continue
        assert msg.type == web.WSMsgType.TEXT, "the socket closed before its error frame"
        frame = json.loads(msg.data)
        if frame.get("type") == "error":
            return frame["code"]


@sync
async def test_the_error_frame_reader_skips_shell_output():
    """A shell can print before its retirement error: the reader must pass over
    that output rather than fail on it."""
    frames = [
        SimpleNamespace(type=web.WSMsgType.BINARY, data=b"$ "),
        SimpleNamespace(type=web.WSMsgType.TEXT, data='{"type":"ready"}'),
        SimpleNamespace(type=web.WSMsgType.TEXT, data='{"type":"error","code":"c1"}'),
    ]
    ws = SimpleNamespace(receive=AsyncMock(side_effect=frames))
    assert await _error_code_of(ws) == "c1"
    closed = SimpleNamespace(
        receive=AsyncMock(return_value=SimpleNamespace(type=web.WSMsgType.CLOSE))
    )
    with pytest.raises(AssertionError, match="closed before"):
        await _error_code_of(closed)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY shell")
@sync
async def test_a_live_local_shell_is_closed_when_its_chat_is_bound(tmp_path, monkeypatch):
    """End to end: the local shell remembers its chat, and retiring that chat's
    shells parks the connected tab with the wrong-machine code."""
    from terminal_handler_helpers import _make_app

    from kiro_crew.dashboard import chat_utils

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    app = _make_app()
    app["state"]._slots = {"main": SimpleNamespace(key="main")}
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect(
            f"/api/ws/terminal/abc123?slot=main&cwd={tmp_path}",
            headers={"Origin": "http://localhost:5476"},
        )
        registry = app["state"]._terminal_sessions
        await async_wait_until(
            lambda: registry.get("abc123") is not None, describe=lambda: "the shell published"
        )
        assert registry["abc123"].chat_slot == "main"
        assert await terminal.retire_local_shells_of_chat(registry, "other") == 0
        assert await terminal.retire_local_shells_of_chat(registry, "main") == 1
        assert "abc123" not in registry
        assert await _error_code_of(ws) == "terminal_wrong_machine"
    finally:
        await _close_with_shells(client, app)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY shell")
@sync
async def test_a_shell_still_opening_when_its_chat_is_bound_is_closed(tmp_path, monkeypatch):
    """The bind's retire skips a shell that is not published yet, so the opener
    reads the placement again once it publishes and closes itself."""
    from terminal_handler_helpers import _make_app

    from kiro_crew.dashboard import chat_utils

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    real_refusal = terminal._local_terminal_refusal
    calls: list[str] = []

    async def bound_right_after_the_first_check(request, slot_name, **kwargs):
        result = await real_refusal(request, slot_name, **kwargs)
        calls.append(result[1])
        if len(calls) == 1:
            # The bind lands while the shell is still a reservation.
            se._choices().set("dashboard:main", "dev")
        return result

    monkeypatch.setattr(terminal, "_local_terminal_refusal", bound_right_after_the_first_check)
    app = _make_app()
    app["state"]._slots = {"main": SimpleNamespace(key="main")}
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect(
            f"/api/ws/terminal/abc123?slot=main&cwd={tmp_path}",
            headers={"Origin": "http://localhost:5476"},
        )
        assert await _error_code_of(ws) == "terminal_wrong_machine"
        assert calls == ["", "terminal_wrong_machine"]
        assert "abc123" not in app["state"]._terminal_sessions
    finally:
        await _close_with_shells(client, app)


@sync
async def test_a_gone_chat_lets_only_its_own_live_local_shell_reconnect(tmp_path, monkeypatch):
    """A dock tab outlives its chat: its live local shell may be taken over, but
    nothing else opens for a chat the gateway does not hold."""
    _home(monkeypatch, tmp_path)
    request = SimpleNamespace(app={"state": SimpleNamespace(_slots={})})
    monkeypatch.setattr(terminal, "_sess_alive", lambda sess: sess.alive)

    def shell(**kw):
        return SimpleNamespace(**{"remote": False, "chat_slot": "gone", "alive": True, **kw})

    assert await terminal._local_terminal_refusal(request, "gone", reattach=shell()) == ("", "")
    for refused in (
        None,
        shell(remote=True),
        shell(chat_slot="other"),
        shell(alive=False),
    ):
        why, code = await terminal._local_terminal_refusal(request, "gone", reattach=refused)
        assert "not loaded yet" in why and code == "terminal_placement_unavailable"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY shell")
@sync
async def test_a_terminal_reconnects_to_its_shell_after_its_chat_is_closed(tmp_path, monkeypatch):
    """End to end: the chat behind a live local shell is closed, and the tab's
    redial reattaches that same shell instead of being refused forever."""
    from terminal_handler_helpers import _make_app

    from kiro_crew.dashboard import chat_utils

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    app = _make_app()
    app["state"]._slots = {"main": SimpleNamespace(key="main")}
    client = TestClient(TestServer(app))
    await client.start_server()
    url = f"/api/ws/terminal/abc123?slot=main&cwd={tmp_path}"
    origin = {"Origin": "http://localhost:5476"}
    try:
        ws = await client.ws_connect(url, headers=origin)
        registry = app["state"]._terminal_sessions
        await async_wait_until(
            lambda: registry.get("abc123") is not None, describe=lambda: "the shell published"
        )
        sess = registry["abc123"]
        app["state"]._slots.pop("main")
        await ws.close()
        ws = await client.ws_connect(url, headers=origin)
        await async_wait_until(
            lambda: sess.ws is not None and not sess.ws.closed and registry.get("abc123") is sess,
            describe=lambda: "the reconnect took over the same shell",
        )
        await ws.send_bytes(b"echo reattached-ok\n")
        seen = b""
        while b"reattached-ok" not in seen:
            msg = await ws.receive(timeout=30)
            if msg.type == web.WSMsgType.TEXT:
                assert json.loads(msg.data).get("type") != "error", msg.data
                continue
            assert msg.type == web.WSMsgType.BINARY, "the socket closed instead of reattaching"
            seen += msg.data
        await ws.close()
        # The shell is gone now: with no live session to take over, the gone
        # chat's redial opens nothing new on this machine.
        await terminal._kill_session(registry.pop("abc123"))
        ws = await client.ws_connect(url, headers=origin)
        assert await _error_code_of(ws) == "terminal_placement_unavailable"
        assert "abc123" not in registry
    finally:
        await _close_with_shells(client, app)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY shell")
@sync
async def test_a_gone_chats_shell_dying_mid_dial_opens_no_new_local_shell(tmp_path, monkeypatch):
    """The reattach is allowed only for THAT shell: when it dies between the check
    and the takeover, the dial is refused rather than spawning a fresh shell."""
    from terminal_handler_helpers import _make_app

    from kiro_crew.dashboard import chat_utils

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    real_openpty = terminal._pty.openpty
    ptys_opened: list[int] = []

    def counting_openpty():
        ptys_opened.append(1)
        return real_openpty()

    monkeypatch.setattr(terminal._pty, "openpty", counting_openpty)
    app = _make_app()
    app["state"]._slots = {"main": SimpleNamespace(key="main")}
    client = TestClient(TestServer(app))
    await client.start_server()
    url = f"/api/ws/terminal/abc123?slot=main&cwd={tmp_path}"
    origin = {"Origin": "http://localhost:5476"}
    try:
        ws = await client.ws_connect(url, headers=origin)
        registry = app["state"]._terminal_sessions
        await async_wait_until(
            lambda: registry.get("abc123") is not None, describe=lambda: "the shell published"
        )
        assert len(ptys_opened) == 1
        app["state"]._slots.pop("main")
        await ws.close()
        real_refusal = terminal._local_terminal_refusal
        real_alive = terminal._sess_alive
        dead = {"now": False}

        async def dies_right_after_the_check(request, slot_name, **kwargs):
            result = await real_refusal(request, slot_name, **kwargs)
            dead["now"] = True
            return result

        monkeypatch.setattr(terminal, "_local_terminal_refusal", dies_right_after_the_check)
        monkeypatch.setattr(terminal, "_sess_alive", lambda s: not dead["now"] and real_alive(s))
        ws = await client.ws_connect(url, headers=origin)
        assert await _error_code_of(ws) == "terminal_placement_unavailable"
        assert "abc123" not in registry
        # Refused before any spawn: no new shell (or login profile) ran for it.
        assert len(ptys_opened) == 1
    finally:
        await _close_with_shells(client, app)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY shell")
@sync
async def test_a_failing_pty_test_leaves_no_shell_running(tmp_path, monkeypatch):
    """The real-PTY tests end in ``_close_with_shells``: an assertion that fails
    while a shell is published still stops that shell and its reader."""
    from terminal_handler_helpers import _make_app

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(terminal, "_is_enabled", lambda request: True)
    app = _make_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    registry = app["state"]._terminal_sessions
    sess = None
    with pytest.raises(AssertionError, match="a test failing mid-way"):
        try:
            await client.ws_connect(
                f"/api/ws/terminal/abc123?cwd={tmp_path}",
                headers={"Origin": "http://localhost:5476"},
            )
            await async_wait_until(
                lambda: registry.get("abc123") is not None, describe=lambda: "the shell published"
            )
            sess = registry["abc123"]
            raise AssertionError("a test failing mid-way")
        finally:
            await _close_with_shells(client, app)
    assert sess is not None and not registry
    assert not terminal._sess_alive(sess)
