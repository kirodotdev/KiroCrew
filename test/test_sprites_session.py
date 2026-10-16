"""Fly.io Sprites session environments, without Fly.io.

A fake Sprites API (aiohttp, loopback) implements the parts the provider uses:
the sprite lifecycle, the exec WebSocket (which really runs the command as a
local child, with the stream-id framing) and the proxy WebSocket (which really
connects to a local port). So the guest worker is installed, started and dialled
exactly as on a real sprite, and every API call is visible to the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kiro_crew.remote_session import wire
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.sprites_session import launch as sprites_launch
from kiro_crew.sprites_session.client import SpritesClient, SpritesError
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

TOKEN = "my-org/tok-1/s3cret-value"
ENGINE = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()\n"
PROFILE = {
    "id": "fly-dev",
    "name": "Fly development",
    "token_secret": "SPRITES_TOKEN",
    "workdir": "",  # set per test to a path under the guest home
}


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


class FakeSprites:
    """The Sprites API surface the provider uses, against local processes."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.sprites: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.exec_urls: list[str] = []
        self.auth: set[str] = set()
        self.children: list[asyncio.subprocess.Process] = []
        self.created = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/sprites", self.list)
        app.router.add_post("/v1/sprites", self.create)
        app.router.add_get("/v1/sprites/{name}", self.get)
        app.router.add_delete("/v1/sprites/{name}", self.delete)
        app.router.add_get("/v1/sprites/{name}/exec", self.exec)
        app.router.add_get("/v1/sprites/{name}/proxy", self.proxy)
        return app

    def _authed(self, request: web.Request) -> bool:
        self.auth.add(request.headers.get("Authorization", ""))
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def list(self, request):
        self.calls.append(("GET", "/v1/sprites"))
        if not self._authed(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"sprites": [], "has_more": False})

    async def create(self, request):
        body = await request.json()
        self.calls.append(("POST", body["name"]))
        if not self._authed(request):
            return web.json_response({}, status=401)
        assert body.get("url_settings") == {"auth": "sprite"}  # never public
        self.created += 1
        self.sprites[body["name"]] = {
            "id": f"sprite-{self.created}",
            "name": body["name"],
            "status": "running",
        }
        return web.json_response(self.sprites[body["name"]], status=201)

    async def get(self, request):
        name = request.match_info["name"]
        self.calls.append(("GET", name))
        if not self._authed(request):
            return web.json_response({}, status=401)
        if name not in self.sprites:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(self.sprites[name])

    async def delete(self, request):
        name = request.match_info["name"]
        self.calls.append(("DELETE", name))
        if not self._authed(request):
            return web.json_response({}, status=401)
        if self.sprites.pop(name, None) is None:
            return web.json_response({}, status=404)
        return web.Response(status=204)

    async def exec(self, request):
        name = request.match_info["name"]
        self.calls.append(("EXEC", name))
        self.exec_urls.append(str(request.url))
        assert self._authed(request) and name in self.sprites
        argv = request.query.getall("cmd")
        ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        env = {
            "HOME": str(self.home),
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.defpath,
            "XDG_RUNTIME_DIR": str(self.home / "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.home / 'runtime' / 'bus'}",
        }
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            cwd=str(self.home),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.children.append(process)

        async def stdin_pump():
            async for msg in ws:
                if msg.type != WSMsgType.BINARY or not msg.data:
                    continue
                if msg.data[0] == 0:
                    process.stdin.write(msg.data[1:])
                    await process.stdin.drain()
                elif msg.data[0] == 4:
                    process.stdin.close()
                    return

        async def out_pump(stream, stream_id):
            while chunk := await stream.read(65536):
                await ws.send_bytes(bytes([stream_id]) + chunk)

        feeder = asyncio.ensure_future(stdin_pump())
        await asyncio.gather(out_pump(process.stdout, 1), out_pump(process.stderr, 2))
        code = await process.wait()
        await ws.send_bytes(bytes([3, code & 0xFF]))
        feeder.cancel()
        await ws.close()
        return ws

    async def proxy(self, request):
        name = request.match_info["name"]
        self.calls.append(("PROXY", name))
        assert self._authed(request) and name in self.sprites
        ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        init = json.loads((await ws.receive()).data)
        reader, writer = await asyncio.open_connection(init["host"], init["port"])
        await ws.send_str(
            json.dumps({"status": "connected", "target": f"{init['host']}:{init['port']}"})
        )

        async def down():
            while chunk := await reader.read(65536):
                await ws.send_bytes(chunk)
            await ws.close()

        task = asyncio.ensure_future(down())
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
        writer.close()
        task.cancel()
        return ws


@contextlib.asynccontextmanager
async def fake_api(tmp_path):
    home = tmp_path / "guest"
    home.mkdir(parents=True, exist_ok=True)
    fake = FakeSprites(home)
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        # Stop guest workers the tests started (setsid'd, so not our children).
        with contextlib.suppress(Exception):
            sessions = home / ".kirocrew-worker" / "sessions"
            for pid_file in sessions.glob("*.pid") if sessions.exists() else []:
                with contextlib.suppress(Exception):
                    os.kill(int(pid_file.read_text().strip()), 15)
        await server.close()


def _envs(tmp_path, base_url, fake, *, token=TOKEN, days=7):
    profile = {**PROFILE, "workdir": str(fake.home / "work")}
    section = SimpleNamespace(sprites_profiles=[profile], archived_retention_days=days)
    (tmp_path / "data").mkdir(exist_ok=True)
    seen: list[str] = []

    def reader(name):
        seen.append(name)
        return token

    envs = sprites_launch.SpritesSessionEnvironments(
        section, tmp_path / "data", token_reader=reader, base_url=base_url
    )
    return envs, seen


# ── The client against the fake API ─────────────────────────────────────


@sync
async def test_exec_carries_stdin_and_returns_output_and_exit_code(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        fake.sprites["s1"] = {"name": "s1", "status": "running"}
        client = SpritesClient(TOKEN, base_url=url)
        big = b"x" * 700_000  # several stdin frames
        code, out, err = await client.exec(
            "s1", ["/bin/sh", "-c", "wc -c; echo oops >&2; exit 3"], big
        )
        assert code == 3 and out.strip() == b"700000" and err.strip() == b"oops"
        assert "s3cret" not in repr(client)


@sync
async def test_a_refused_token_says_so_without_echoing_it(tmp_path):
    async with fake_api(tmp_path) as (_fake, url):
        with pytest.raises(SpritesError, match="token was refused") as info:
            await SpritesClient("my-org/bad/nope", base_url=url).list_sprites()
        assert "nope" not in str(info.value)


# ── The provider, end to end ────────────────────────────────────────────


@sync
async def test_a_session_gets_a_sprite_and_its_worker_is_dialled_through_the_proxy(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, seen = _envs(tmp_path, url, fake)
        prepared = await envs.prepare("dashboard:chat-1", "fly-dev")
        row = envs.registry.get("dashboard:chat-1")
        assert row is not None and row.name.startswith("kirocrew-") and len(row.name) == 25
        assert prepared.workspace == row.name and prepared.profile_id == "fly-dev"
        assert (fake.home / "work").is_dir()  # the workdir is created in the sprite
        assert seen and set(seen) == {"SPRITES_TOKEN"}  # the vault entry the profile names
        launch = prepared.launch
        await launch.install()
        assert (fake.home / ".kirocrew-worker" / "worker.py").exists()
        worker_id = launch.new_worker_id()
        secret = wire.new_secret()
        port = await launch.start_worker(worker_id, [sys.executable, "-u", "-c", ENGINE], secret)
        link = RemoteLink(
            worker_id,
            secret,
            launch.dial(port),
            keepalive_seconds=1,
            stall_seconds=5,
            lease_seconds=20,
        )
        try:
            link.start()
            await link.wait_connected(20)
            for size in (10, 1017, 200_000):
                line = json.dumps({"pad": "x" * size}).encode() + b"\n"
                await link.write(line)
                assert await asyncio.wait_for(link.stdout.readline(), 30) == line
        finally:
            await link.aclose()
            with contextlib.suppress(Exception):
                await launch.stop_worker(worker_id)
        # The session secret travelled on stdin, never in an exec URL, and the
        # token only ever as the Authorization header.
        assert not any(secret.hex() in u for u in fake.exec_urls)
        assert not any(TOKEN in u for u in fake.exec_urls)
        assert fake.auth == {f"Bearer {TOKEN}"}
        # Reused while it lives, not recreated.
        again = await envs.prepare("dashboard:chat-1", "fly-dev")
        assert again.workspace == row.name
        assert [c for c in fake.calls if c[0] == "POST"] == [("POST", row.name)]


@sync
async def test_a_sprite_deleted_outside_is_replaced_by_a_fresh_one(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake)
        await envs.prepare("dashboard:chat-2", "fly-dev")
        old = envs.registry.get("dashboard:chat-2").name
        del fake.sprites[old]
        await envs.prepare("dashboard:chat-2", "fly-dev")
        new = envs.registry.get("dashboard:chat-2").name
        assert new != old and new in fake.sprites


@sync
async def test_no_token_refuses_before_any_sprite_exists(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake, token="")
        with pytest.raises(SessionEnvironmentUnavailable, match="SPRITES_TOKEN"):
            await envs.prepare("dashboard:chat-3", "fly-dev")
        assert fake.calls == [] and envs.registry.get("dashboard:chat-3") is None


@sync
async def test_a_failed_create_leaves_no_sprite_or_row(tmp_path, monkeypatch):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake)

        async def boom(self, name):
            raise SpritesError("Sprites create failed (HTTP 500)")

        monkeypatch.setattr(SpritesClient, "create_sprite", boom)
        with pytest.raises(SpritesError):
            await envs.prepare("dashboard:chat-4", "fly-dev")
        assert envs.registry.get("dashboard:chat-4") is None


@sync
async def test_archiving_keeps_the_sprite_for_the_retention_then_deletes_it(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake, days=7)
        await envs.prepare("dashboard:chat-5", "fly-dev")
        name = envs.registry.get("dashboard:chat-5").name
        assert await envs.archive_session("dashboard:chat-5") is True
        due = envs.registry.get("dashboard:chat-5").deletion_due_at
        assert due and name in fake.sprites
        assert await envs.sweep_due() == 0  # not yet
        # Reopening before the deadline keeps it and cancels the deletion.
        await envs.prepare("dashboard:chat-5", "fly-dev")
        assert envs.registry.get("dashboard:chat-5").deletion_due_at == ""
        await envs.archive_session("dashboard:chat-5")
        later = datetime.now(timezone.utc) + timedelta(days=8)
        assert await envs.sweep_due(now=later) == 1
        assert name not in fake.sprites and envs.registry.get("dashboard:chat-5") is None
        # A session with no sprite is a no-op.
        assert await envs.archive_session("dashboard:local") is False


@sync
async def test_zero_days_retention_deletes_the_sprite_at_once(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake, days=0)
        await envs.prepare("dashboard:chat-6", "fly-dev")
        gone = envs.registry.get("dashboard:chat-6").name
        assert await envs.archive_session("dashboard:chat-6") is True
        assert gone not in fake.sprites and envs.registry.get("dashboard:chat-6") is None


@sync
async def test_a_sweep_spares_a_sprite_reopened_after_its_scan(tmp_path, monkeypatch):
    """The sweep re-reads the row under the session's lifecycle lock before deleting."""
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake)
        await envs.prepare("dashboard:chat-9", "fly-dev")
        name = envs.registry.get("dashboard:chat-9").name
        await envs.archive_session("dashboard:chat-9")
        later = datetime.now(timezone.utc) + timedelta(days=8)
        due = envs.registry.due(later)
        # The scan saw the row due; the session is reopened before the delete.
        monkeypatch.setattr(envs.registry, "due", lambda moment: due)
        await envs.prepare("dashboard:chat-9", "fly-dev")
        assert await envs.sweep_due(now=later) == 0
        assert name in fake.sprites and envs.registry.get("dashboard:chat-9") is not None


@sync
async def test_a_sprite_answering_under_its_name_with_another_id_is_refused(tmp_path):
    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake)
        prepared = await envs.prepare("dashboard:chat-10", "fly-dev")
        assert envs.registry.get("dashboard:chat-10").resource_id == "sprite-1"
        # Deleted outside Kiro Crew and another sprite created under the same name.
        fake.sprites[prepared.workspace]["id"] = "sprite-someone-else"
        with pytest.raises(SessionEnvironmentUnavailable, match="another machine"):
            await envs.prepare("dashboard:chat-10", "fly-dev")
        assert [c for c in fake.calls if c[0] == "DELETE"] == []


@sync
async def test_the_files_panel_and_terminal_reach_the_sprite_without_creating_it(
    tmp_path, monkeypatch
):
    from kiro_crew.coder import session_browse, session_launch
    from kiro_crew.config import live, paths

    async with fake_api(tmp_path) as (fake, url):
        envs, _ = _envs(tmp_path, url, fake)
        home = tmp_path / "data"
        monkeypatch.setattr(paths, "data_home", lambda: home)
        monkeypatch.setattr(
            live, "snapshot", lambda: SimpleNamespace(session_environments=envs._config)
        )
        monkeypatch.setattr(sprites_launch, "_vault_token", lambda name: TOKEN)
        monkeypatch.setattr(sprites_launch.SpritesClient, "__init__", _client_init(url))
        session_launch.SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(
            "dashboard:chat-7", "fly-dev"
        )
        # Before the sprite exists, nothing is created to answer the panel.
        with pytest.raises(session_browse.WorkspaceNotRunning):
            await session_browse.browse_session("dashboard:chat-7", "list", ".")
        assert not any(c[0] == "POST" for c in fake.calls)
        prepared = await envs.prepare("dashboard:chat-7", "fly-dev")
        (fake.home / "work" / "notes.txt").write_text("hi\n")
        await prepared.launch.install()
        listing = await session_browse.browse_session("dashboard:chat-7", "list", ".")
        assert "notes.txt" in json.dumps(listing)
        # The terminal opens the bridge, naming the vault secret, never the token.
        argv = await session_browse.remote_terminal_argv("dashboard:chat-7")
        assert "kiro_crew.sprites_session.terminal" in argv
        assert argv[argv.index("--sprite") + 1] == prepared.workspace
        assert argv[argv.index("--token-secret") + 1] == "SPRITES_TOKEN"
        assert TOKEN not in " ".join(argv)


def _client_init(url):
    original = SpritesClient.__init__

    def init(self, token, *, base_url=url):
        original(self, token, base_url=url)

    return init


@sync
async def test_a_sprites_choice_is_prepared_by_the_sprites_provider(monkeypatch, tmp_path):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live, paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    section = SimpleNamespace(coder_url="", profiles=[], sprites_profiles=[PROFILE])
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=section))
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "dashboard:chat-8", "fly-dev"
    )
    seen = []

    async def prepare(self, key, profile_id):
        seen.append((key, profile_id))
        return "prepared"

    monkeypatch.setattr(sprites_launch.SpritesSessionEnvironments, "prepare", prepare)
    assert await session_launch.prepare_for_session("dashboard:chat-8") == "prepared"
    assert seen == [("dashboard:chat-8", "fly-dev")]


# ── Config and dashboard wiring ─────────────────────────────────────────


def test_sprites_profiles_validate_and_never_hold_a_token():
    from kiro_crew.config import session_environments as se

    good = {**PROFILE, "workdir": "/workspace/work"}
    assert se.validate_sprites_profiles([good]) == [good]
    with pytest.raises(se.SessionEnvironmentsError):
        se.validate_sprites_profiles([{**good, "token": TOKEN}])  # no inline token key
    # A refusal names the Sprites list, not the Coder one it shares a check with.
    with pytest.raises(se.SessionEnvironmentsError, match=r"^sprites_profiles\[0\]\.name"):
        se.validate_sprites_profiles([{**good, "name": ""}])
    with pytest.raises(se.SessionEnvironmentsError, match="token_secret"):
        se.validate_sprites_profiles([{**good, "token_secret": "my-org/x/y"}])
    with pytest.raises(se.SessionEnvironmentsError, match="both a Coder and a Sprites"):
        se.validate_session_environments(
            {
                "coder_url": "https://c.example.com",
                "profiles": [{"id": "fly-dev", "name": "c", "template": "t", "workdir": "/w"}],
                "sprites_profiles": [good],
            }
        )
    # A hand-edited invalid entry is dropped at load, the valid one kept.
    assert se.load_sprites_profiles({"sprites_profiles": [good, {"id": "x"}]}) == [good]


def test_sprites_profiles_are_offered_without_a_coder_url(monkeypatch, tmp_path):
    from kiro_crew.config import loader, paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    good = {**PROFILE, "workdir": "/workspace/work"}
    section = SimpleNamespace(coder_url="", profiles=[], sprites_profiles=[good])
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(session_environments=section)),
    )
    assert handlers._profiles() == [
        {
            "id": "fly-dev",
            "name": "Fly development",
            "provider": "sprites",
            "workdir": "/workspace/work",
        }
    ]


@sync
async def test_the_readiness_check_proves_each_token_and_touches_no_sprite(tmp_path, monkeypatch):
    from kiro_crew.config import paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    async with fake_api(tmp_path) as (fake, url):
        monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
        monkeypatch.setattr(sprites_launch.SpritesClient, "__init__", _client_init(url))
        tokens = {"SPRITES_TOKEN": TOKEN, "OTHER": "my-org/bad/x"}
        monkeypatch.setattr(sprites_launch, "_vault_token", lambda name: tokens.get(name, ""))
        section = SimpleNamespace(
            sprites_profiles=[
                {**PROFILE, "workdir": "/w"},
                {**PROFILE, "id": "fly-bad", "token_secret": "OTHER", "workdir": "/w"},
                {**PROFILE, "id": "fly-none", "token_secret": "MISSING", "workdir": "/w"},
            ]
        )
        rows = await handlers._check_sprites(section)
        assert rows[0] == {"id": "fly-dev", "ok": True}
        assert rows[1]["ok"] is False and "refused" in rows[1]["error"]
        assert rows[2]["ok"] is False and "MISSING" in rows[2]["error"]
        assert {c[0] for c in fake.calls} == {"GET"}  # list only: no create, exec or delete


# ── The dashboard terminal bridge ───────────────────────────────────────


def test_the_terminal_argv_names_the_secret_never_the_token():
    from kiro_crew.sprites_session.terminal import bridge_argv

    argv = bridge_argv("kirocrew-0123456789abcdef", "SPRITES_TOKEN", "/workspace/work")
    assert argv[-6:] == [
        "--sprite",
        "kirocrew-0123456789abcdef",
        "--token-secret",
        "SPRITES_TOKEN",
        "--dir",
        "/workspace/work",
    ]
    assert TOKEN not in " ".join(argv)
    for bad in ("--api-url=http://evil", "other-sprite", "kirocrew-XYZ"):
        with pytest.raises(ValueError):
            bridge_argv(bad, "SPRITES_TOKEN", "")
    with pytest.raises(ValueError):
        bridge_argv("kirocrew-0123456789abcdef", "--dir", "")


@sync
async def test_the_terminal_bridge_carries_a_tty_session_and_resize(tmp_path, monkeypatch):
    """The bridge, as the PTY child, relays raw bytes and resizes over a TTY exec."""
    from aiohttp import WSMsgType, web

    seen: dict = {"query": None, "resize": None, "auth": None}
    resized = asyncio.Event()

    async def exec_tty(request):
        seen["query"] = dict(request.query)
        seen["auth"] = request.headers.get("Authorization")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"$ ")
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                info = json.loads(msg.data)
                if info.get("type") == "resize":
                    seen["resize"] = (info["cols"], info["rows"])
                    resized.set()
            elif msg.type == WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + msg.data)
                if b"exit" in msg.data:
                    await ws.send_str(json.dumps({"type": "exit", "exit_code": 3}))
                    break
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/v1/sprites/{name}/exec", exec_tty)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    master, worker = os.openpty()  # the panel's PTY: worker is the bridge's stdio
    import fcntl
    import struct
    import termios

    fcntl.ioctl(worker, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    vault = tmp_path / "vault"
    vault.mkdir()
    # The bridge reads the token from the vault in its own process.
    from kiro_crew.secrets.vault import SecretVault

    env = {**os.environ, "KIROCREW_HOME": str(vault)}
    monkeypatch.setenv("KIROCREW_HOME", str(vault))
    SecretVault(vault).set_sync("SPRITES_TOKEN", TOKEN)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.sprites_session.terminal",
        "--sprite",
        "kirocrew-0123456789abcdef",
        "--token-secret",
        "SPRITES_TOKEN",
        "--dir",
        "/workspace/work",
        "--api-url",
        base,
        stdin=worker,
        stdout=worker,
        stderr=worker,
        env=env,
        start_new_session=True,
    )
    os.close(worker)
    loop = asyncio.get_running_loop()

    async def read_until(marker: bytes) -> bytes:
        out = b""
        while marker not in out:
            out += await await_until(
                loop.run_in_executor(None, os.read, master, 4096),
                what=f"terminal output containing {marker!r}",
            )
        return out

    try:
        assert b"$ " in await read_until(b"$ ")
        os.write(master, b"ls")
        assert b"echo:ls" in await read_until(b"echo:ls")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.killpg(proc.pid, signal.SIGWINCH)
        await await_until(resized, what="the terminal resize reaches the guest")
        os.write(master, b"exit")
        assert (
            await await_until(asyncio.ensure_future(proc.wait()), what="the terminal child exits")
            == 3
        )
    finally:
        if proc.returncode is None:
            proc.kill()
        os.close(master)
        await server.close()
    q = seen["query"]
    assert q["tty"] == "true" and q["cols"] == "100" and q["rows"] == "30"
    assert q["dir"] == "/workspace/work"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["resize"] == (120, 40)


def test_the_bridge_refuses_a_non_loopback_api_override():
    from kiro_crew.sprites_session.terminal import main

    assert (
        main(
            [
                "--sprite",
                "kirocrew-0123456789abcdef",
                "--token-secret",
                "X",
                "--api-url",
                "https://evil.example",
            ]
        )
        == 2
    )
