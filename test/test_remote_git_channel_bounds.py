"""Git-channel receive bounds, repository names and LFS routing."""

from __future__ import annotations

import asyncio
import base64
import sys

import pytest

from kiro_crew.remote_session import git_gateway


@pytest.mark.asyncio
async def test_an_upload_flows_on_credit_and_never_buffers_past_its_window():
    """A large upload into a slow upstream: the guest sends only on credit, the
    gateway grants one per piece it takes, so the queue stays within the window
    while every byte still arrives."""
    from test_remote_git_proxy import FrameLink

    from kiro_crew.testing.wait import async_wait_until

    link = FrameLink()
    received = 0
    peak = 0
    complete = asyncio.Event()

    class SlowChannel(git_gateway.GitProxyChannel):
        async def _forward(self, rid, frame, request, credential, body, head=b""):
            nonlocal received
            async for piece in self._drain(rid, body):
                received += len(piece)
                await asyncio.sleep(0)  # a slow upstream
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
    piece = base64.b64encode(b"x" * git_gateway._CHUNK).decode()
    serve = asyncio.create_task(channel.serve())
    try:
        link.guest(
            {"t": "req", "id": 1, "method": "POST", "path": "/github.com/a/b/git-upload-pack"}
        )
        sent = 0
        while sent < count:
            # The guest's rule: at most UPLOAD_WINDOW frames beyond the credit granted.
            await async_wait_until(
                lambda: sent < git_gateway.UPLOAD_WINDOW + len(link.credits),
                describe=lambda: f"no credit after {sent} frames",
            )
            link.guest({"t": "body", "id": 1, "b": piece})
            sent += 1
            await asyncio.sleep(0)
            if 1 in channel._requests:
                peak = max(peak, channel._requests[1].qsize())
        link.guest({"t": "end", "id": 1})
        await asyncio.wait_for(complete.wait(), 30)
        assert received == count * git_gateway._CHUNK
        assert link.credits == [{"t": "credit", "id": 1}] * count
        assert peak <= git_gateway.UPLOAD_WINDOW
        assert not any(f.get("t") == "err" for f in link.frames)
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)


@pytest.mark.asyncio
async def test_a_stalled_upload_leaves_its_cancel_and_other_requests_readable():
    """An upstream that stops reading a push with its window full: the reader
    still takes this request's cancel and serves another request meanwhile."""
    from test_remote_git_proxy import FrameLink

    from kiro_crew.testing.wait import async_wait_until

    link = FrameLink()
    stalled = asyncio.Event()
    cancelled = asyncio.Event()

    class StalledChannel(git_gateway.GitProxyChannel):
        async def _forward(self, rid, frame, request, credential, body, head=b""):
            if rid == 1:
                stalled.set()
                try:
                    await asyncio.Event().wait()  # upstream never reads
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            await self._send({"t": "resp", "id": rid, "status": 200, "headers": {}})
            await self._send({"t": "end", "id": rid})

    async def credential(host):
        return "Basic dTpw"

    channel = StalledChannel(
        link,
        "dashboard:stall",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *_: True,
    )
    serve = asyncio.create_task(channel.serve())
    try:
        link.guest(
            {"t": "req", "id": 1, "method": "POST", "path": "/github.com/a/b/git-upload-pack"}
        )
        await asyncio.wait_for(stalled.wait(), 30)
        for _ in range(git_gateway.UPLOAD_WINDOW):
            link.guest({"t": "body", "id": 1, "b": base64.b64encode(b"x").decode()})
        await async_wait_until(lambda: channel._requests[1].qsize() == git_gateway.UPLOAD_WINDOW)
        link.get(2, "/github.com/a/b/info/refs?service=git-upload-pack")
        await async_wait_until(lambda: {"t": "end", "id": 2} in link.frames)
        link.guest({"t": "cancel", "id": 1})
        await asyncio.wait_for(cancelled.wait(), 30)
        await async_wait_until(lambda: 1 not in channel._tasks)
    finally:
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)


def test_the_guest_and_the_gateway_agree_on_the_upload_window():
    from kiro_crew.remote_session import git_proxy

    assert git_proxy.UPLOAD_WINDOW == git_gateway.UPLOAD_WINDOW


@pytest.mark.asyncio
async def test_a_paused_git_reader_does_not_trip_the_stall_check(monkeypatch):
    """The git reader's backpressure goes through the base link's consumer
    gate, so a busy reader is not mistaken for a silent connection."""
    from kiro_crew.remote_session import link as link_module
    from kiro_crew.testing.wait import async_wait_until

    async def unused_dial():
        raise AssertionError("the wire is attached in memory")

    reads = []

    def clock():
        # Every reading is one second later: ten readings are ten quiet seconds.
        reads.append(None)
        return float(len(reads))

    link = git_gateway.GitProxyLink(
        "git-stall",
        b"s" * 32,
        unused_dial,
        clock=clock,
        stall_seconds=2.0,
        keepalive_seconds=1.0,
        lease_seconds=2.0,
    )
    closed = []

    class Writer:
        def close(self):
            closed.append(True)

    link._writer = Writer()
    link.stdout.feed_data(b"x" * (3 * 64 * 1024))  # past the reader's limit
    assert not link._consumer.open.is_set()
    with monkeypatch.context() as mp:
        mp.setattr(link, "_send", lambda data: None)
        mp.setattr(link_module, "TICK_SECONDS", 0.0)
        start = len(reads)
        ticker = asyncio.ensure_future(link._tick())
        try:
            await async_wait_until(
                lambda: len(reads) - start >= 20 or ticker.done(),
                describe=lambda: f"clock reads: {len(reads) - start}",
            )
            assert not closed and not ticker.done()  # well past stall_seconds, but paused
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")
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
        process, serve, port_file, link = await _run_channel(tmp_path, upstream)
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
        await _in_thread(_git, "push", "origin", "HEAD:feat/lfs", cwd=clone, env=env)
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
