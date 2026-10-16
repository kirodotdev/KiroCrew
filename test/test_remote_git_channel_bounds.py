"""Git-channel receive bounds and repository-state/error distinctions."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import git_gateway, wire, workspace_tools


@pytest.mark.asyncio
async def test_full_body_queue_stops_wire_acknowledgements_and_resumes(monkeypatch):
    async def unused_dial():
        raise AssertionError("the wire is attached in memory")

    link = git_gateway.GitProxyLink("git-bounds", b"s" * 32, unused_dial)
    worker = wire.Endpoint(wire.WORKER)
    attachment = wire.Attachment(b"k" * 32, 0)
    link.ep.attach(attachment, 0)
    worker.attach(attachment, 0)
    monkeypatch.setattr(link, "_send", lambda data: worker.feed(data, 0))
    blocked = asyncio.Event()
    pause = link._receive_window.pause_reading

    def paused():
        pause()
        blocked.set()

    monkeypatch.setattr(link._receive_window, "pause_reading", paused)
    release = asyncio.Event()
    complete = asyncio.Event()
    received = 0

    class SlowChannel(git_gateway.GitProxyChannel):
        async def _forward(self, rid, frame, request, credential, body):
            nonlocal received
            await release.wait()
            async for piece in self._drain(body):
                received += len(piece)
            complete.set()

    async def credential(host):
        return "Basic dTpw"

    channel = SlowChannel(
        link,
        "dashboard:bounds",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *_: True,
    )
    count = 200
    piece = b"x" * git_gateway._CHUNK

    async def send(frame):
        data = worker.write((json.dumps(frame) + "\n").encode(), 0)
        for offset in range(0, len(data), 65536):
            await link._consume(data[offset : offset + 65536])

    async def produce():
        await send(
            {"t": "req", "id": 1, "method": "POST", "path": "/github.com/a/b/git-upload-pack"}
        )
        for _ in range(count):
            await send({"t": "body", "id": 1, "b": base64.b64encode(piece).decode()})
        await send({"t": "end", "id": 1})

    serve = asyncio.create_task(channel.serve())
    producer = asyncio.create_task(produce())
    try:
        await asyncio.wait_for(blocked.wait(), 30)
        from kiro_crew.testing.wait import async_wait_until

        await async_wait_until(
            lambda: channel._requests[1].full() and not link._receive_window.ready.is_set()
        )
        assert not producer.done()
        assert channel._requests[1].full()
        accepted = link.ep.recv_next
        # Timer acknowledgements may cover buffered bytes, never the pending feed.
        link._send(link.ep.tick(1))
        assert link.ep.recv_next == accepted < worker.next_seq
        assert worker.unacked_bytes > 0
        assert len(link.stdout._buffer) <= 2 * link.stdout._limit + 65536
        release.set()
        await asyncio.wait_for(producer, 30)
        await asyncio.wait_for(complete.wait(), 30)
        assert received == count * len(piece)
        assert link.ep.recv_next == worker.next_seq
    finally:
        release.set()
        producer.cancel()
        serve.cancel()
        await asyncio.gather(producer, serve, return_exceptions=True)
        await link.aclose()


@pytest.mark.parametrize("repo", [".github", ".dotfiles", "a..b"])
def test_leading_dot_and_embedded_dots_are_repository_names(repo):
    request = git_gateway.parse_request(
        "GET", f"/github.com/acme/{repo}/info/refs?service=git-upload-pack"
    )
    assert request.repo_path == f"acme/{repo}"
    assert request.upstream_url() == (
        f"https://github.com/acme/{repo}/info/refs?service=git-upload-pack"
    )


@pytest.mark.parametrize("op", ["git_status", "git_log", "git_diff"])
@pytest.mark.parametrize("failure", ["missing", "permission", "corruption", "timeout"])
def test_git_subprocess_failures_are_errors_not_repository_states(
    tmp_path, monkeypatch, op, failure
):
    def run(argv, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        if failure == "missing":
            raise FileNotFoundError("git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        error = (
            b"fatal: permission denied" if failure == "permission" else b"fatal: bad object HEAD"
        )
        return SimpleNamespace(returncode=128, stdout=b"", stderr=error)

    monkeypatch.setattr(workspace_tools.subprocess, "run", run)
    result = workspace_tools.browse(workspace_tools.Workspace(str(tmp_path)), {"op": op})
    assert result["code"] == "git_failed"
    assert result["error_text"]
    assert "output" not in result


@pytest.mark.parametrize("op", ["git_status", "git_log", "git_diff"])
def test_only_not_repository_diagnostic_is_a_non_repo_state(tmp_path, monkeypatch, op):
    monkeypatch.setattr(
        workspace_tools.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=128,
            stdout=b"",
            stderr=b"fatal: not a git repository (or any of the parent directories): .git\n",
        ),
    )
    result = workspace_tools.browse(workspace_tools.Workspace(str(tmp_path)), {"op": op})
    assert result == {"root": str(tmp_path.resolve()), "git": False}


def test_unborn_history_is_empty_not_a_process_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workspace_tools.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=128,
            stdout=b"",
            stderr=b"fatal: your current branch 'main' does not have any commits yet\n",
        ),
    )
    result = workspace_tools.browse(workspace_tools.Workspace(str(tmp_path)), {"op": "git_log"})
    assert result == {"root": str(tmp_path.resolve()), "git": True, "output": ""}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_unborn_diff_includes_staged_files(tmp_path, monkeypatch, object_format):
    import shutil

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-config"))
    subprocess.run(
        [git, "init", f"--object-format={object_format}", "-b", "main", str(tmp_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "new.txt").write_text("first content\n")
    subprocess.run([git, "add", "new.txt"], cwd=tmp_path, check=True)
    ws = workspace_tools.Workspace(str(tmp_path))
    assert workspace_tools.browse(ws, {"op": "git_log"})["output"] == ""
    result = workspace_tools.browse(ws, {"op": "git_diff", "path": "new.txt"})
    assert result["git"] is True and "+first content" in result["output"]
    assert "error_text" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["objects/batch", "locks/verify"])
@pytest.mark.parametrize(
    "repo",
    [
        "github.com/acme/.github.git",
        "gitlab.com/g/p.git",
        "bitbucket.org/t/r.git",
        "dev.azure.com/o/_git/r",
    ],
)
async def test_lfs_returns_to_https_without_reading_credentials(repo, op):
    from test_remote_git_proxy import FrameLink

    from kiro_crew.testing.wait import async_wait_until

    link = FrameLink()

    async def never_read(host):
        raise AssertionError("LFS must not read a gateway credential")

    judged = []

    def egress(key, host, agent):
        judged.append((key, host))
        return True

    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:lfs",
        credential_source=never_read,
        agent="researcher",
        egress_permitted=egress,
    )
    serve = asyncio.create_task(channel.serve())
    try:
        link.guest({"t": "req", "id": 1, "method": "POST", "path": f"/{repo}/info/lfs/{op}"})
        link.guest({"t": "end", "id": 1})
        await async_wait_until(lambda: len(link.frames) == 2)
        assert link.frames == [
            {
                "t": "resp",
                "id": 1,
                "status": 307,
                "headers": {"Location": f"https://{repo}/info/lfs/{op}"},
            },
            {"t": "end", "id": 1},
        ]
        assert judged == [("dashboard:lfs", repo.split("/")[0])]
        assert channel._credentials == {} and channel._http is None
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)


@pytest.mark.asyncio
async def test_real_lfs_clone_and_push_bypass_the_smart_http_rewrite(tmp_path, monkeypatch):
    import hashlib
    import shutil

    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from test_remote_git_proxy import (
        HttpBackendUpstream,
        _client_env,
        _git,
        _in_thread,
        _run_channel,
        _seed_bare,
        _stop,
    )

    if shutil.which("git-lfs") is None:
        pytest.skip("git-lfs is required for the real LFS transfer")
    root = tmp_path / "upstream"
    bare = _seed_bare(tmp_path, root / "github.com" / "acme" / "widgets.git")
    initial = b"an LFS object\n"
    oid = hashlib.sha256(initial).hexdigest()
    objects = {oid: initial}
    operations = []
    headers_seen = []
    server = None

    async def batch(request):
        headers_seen.append(dict(request.headers))
        payload = await request.json()
        operation = payload["operation"]
        operations.append(operation)
        assert server is not None
        return web.json_response(
            {
                "transfer": "basic",
                "objects": [
                    {
                        **obj,
                        "actions": {
                            operation: {"href": str(server.make_url("/object/" + obj["oid"]))}
                        },
                    }
                    for obj in payload["objects"]
                ],
            },
            content_type="application/vnd.git-lfs+json",
        )

    async def locks(request):
        return web.json_response({"ours": [], "theirs": []})

    async def transfer(request):
        headers_seen.append(dict(request.headers))
        key = request.match_info["oid"]
        if request.method == "PUT":
            content = await request.read()
            assert hashlib.sha256(content).hexdigest() == key
            objects[key] = content
            return web.Response(status=200)
        return web.Response(body=objects[key])

    app = web.Application()
    app.router.add_post("/info/lfs/objects/batch", batch)
    app.router.add_post("/info/lfs/locks/verify", locks)
    app.router.add_route("*", "/object/{oid}", transfer)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    original_url = git_gateway.GitRequest.upstream_url
    monkeypatch.setattr(
        git_gateway.GitRequest,
        "upstream_url",
        lambda self: (
            str(server.make_url("/" + self.op)).replace("127.0.0.1", "localhost")
            if self.is_lfs
            else original_url(self)
        ),
    )
    upstream = HttpBackendUpstream(root)
    process = serve = None
    try:
        process, serve, port_file, link, _ = await _run_channel(tmp_path, upstream)
        env = _client_env(tmp_path, port_file)
        seed = tmp_path / "lfs-seed"
        await _in_thread(_git, "clone", str(bare), str(seed), cwd=tmp_path, env=env)
        (seed / ".gitattributes").write_text("*.dat filter=lfs diff=lfs merge=lfs -text\n")
        (seed / "asset.dat").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(initial)}\n"
        )
        await _in_thread(_git, "add", ".gitattributes", "asset.dat", cwd=seed, env=env)
        await _in_thread(_git, "commit", "-m", "LFS pointer", cwd=seed, env=env)
        await _in_thread(_git, "push", "origin", "main", cwd=seed, env=env)
        await _in_thread(_git, "lfs", "install", "--skip-repo", cwd=tmp_path, env=env)
        clone = tmp_path / "lfs-clone"
        await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        assert (clone / "asset.dat").read_bytes() == initial
        changed = b"a new LFS object\n"
        (clone / "asset.dat").write_bytes(changed)
        await _in_thread(_git, "lfs", "install", "--local", cwd=clone, env=env)
        await _in_thread(_git, "add", "asset.dat", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "LFS upload", cwd=clone, env=env)
        await _in_thread(_git, "push", "origin", "main", cwd=clone, env=env)
        assert objects[hashlib.sha256(changed).hexdigest()] == changed
        assert "download" in operations and "upload" in operations
        assert headers_seen and all(
            "Authorization" not in h and "X-Kiro-Git" not in h for h in headers_seen
        )
        assert all("info/lfs" not in c["url"] for c in upstream.calls)
    finally:
        if process is not None:
            await _stop(process, serve)
        await server.close()


@pytest.mark.asyncio
async def test_runtime_constructs_the_backpressured_git_link(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_remote_session_launch import _launch

    launch = _launch(tmp_path, "git-factory")
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=1))
    monkeypatch.setattr(git_gateway.RemoteLink, "start", lambda self: None)
    await launch.start_git_proxy("dashboard:factory", agent="researcher")
    _, link, serve = launch.git_proxy
    try:
        assert isinstance(link, git_gateway.GitProxyLink)
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)
        await link.aclose()
