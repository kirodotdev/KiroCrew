"""Git proxy admission, caller capability and workspace-relative browsing."""

from pathlib import Path

import pytest
from test_remote_git_credential_isolation import git_workdir as _git_workdir

from kiro_crew.remote_session import git_gateway, workspace_tools
from kiro_crew.testing.wait import await_until

git_workdir = _git_workdir


@pytest.fixture(autouse=True)
def _fresh_approvals(_floor_monkeypatch):
    _floor_monkeypatch.setattr(git_gateway, "_PUSH_APPROVED", {})


def test_status_paths_diff_from_a_nested_workdir(git_workdir):
    ws, git = git_workdir
    root = Path(ws.root)
    (root / "app").mkdir()
    tracked = root / "app" / "tracked.txt"
    outside = root / "outside.txt"
    tracked.write_text("before\n")
    outside.write_text("outside before\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "files")
    tracked.write_text("after\n")
    outside.write_text("outside after\n")
    (root / "app" / "new.txt").write_text("new file\n")
    nested = workspace_tools.Workspace(str(root / "app"))
    status = workspace_tools.browse(nested, {"op": "git_status"})
    assert " M app/tracked.txt\0" in status["output"]
    assert "?? app/new.txt\0" in status["output"]
    for name, expected in (("app/tracked.txt", "+after"), ("app/new.txt", "+new file")):
        diff = workspace_tools.browse(nested, {"op": "git_diff", "path": name})
        assert diff["git"] is True
        assert expected in diff["output"]
    for name in ("outside.txt", "app/../../outside.txt"):
        refused = workspace_tools.browse(nested, {"op": "git_diff", "path": name})
        assert "outside the workspace" in refused["error"]
    all_diff = workspace_tools.browse(nested, {"op": "git_diff", "path": "."})
    assert "+after" in all_diff["output"]
    assert "outside after" not in all_diff["output"]


def test_proxy_requires_its_own_capability_and_exact_host():
    import http.client
    import io
    import threading

    from kiro_crew.remote_session import git_proxy

    class ReplyingGateway(git_proxy.Gateway):
        def send(self, frame):
            super().send(frame)
            if frame["t"] == "end":
                self._queues[frame["id"]].put({"t": "err", "status": 418, "message": "accepted"})

    out = io.StringIO()
    gateway = ReplyingGateway(out=out)
    secret = "a" * 64
    handler = type("Bound", (git_proxy.Handler,), {"gateway": gateway, "secret": secret})
    server = git_proxy.Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    port = server.server_address[1]

    def fetch(headers):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(
                "GET", "/github.com/a/r/info/refs?service=git-upload-pack", headers=headers
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        for headers in (
            {},
            {"Authorization": "Bearer " + "b" * 64},
            {"Authorization": f"Bearer {secret}", "Host": "evil.invalid"},
        ):
            status, body = fetch(headers)
            assert status == 403 and secret.encode() not in body
            assert not out.getvalue()
        status, body = fetch({"Authorization": f"Bearer {secret}"})
        assert status == 418 and b"accepted" in body
        assert secret not in out.getvalue()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_channel_caps_pending_approvals_and_releases_admission():
    import asyncio

    from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream

    from kiro_crew.remote_session import git_gateway
    from kiro_crew.testing.wait import async_wait_until

    link = FrameLink()
    approval = asyncio.Event()

    async def approve(*args):
        await approval.wait()
        return True

    async def credential(host):
        return HEADER

    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:bounded",
        approve=approve,
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *args: True,
        http_session=HttpBackendUpstream(Path("unused"), status=404),
    )
    serve = asyncio.create_task(channel.serve())
    limit = git_gateway.MAX_LIVE_REQUESTS
    path = "/github.com/acme/repo/info/refs?service=git-receive-pack"
    try:
        for rid in range(1, limit + 2):
            link.get(rid, path)
        await async_wait_until(lambda: any(f["id"] == limit + 1 for f in link.frames))
        assert len(channel._tasks) == len(channel._requests) == limit
        assert next(f for f in link.frames if f["id"] == limit + 1)["status"] == 503
        approval.set()
        await async_wait_until(lambda: not channel._tasks and not channel._requests)
        link.get(limit + 2, path)
        await async_wait_until(
            lambda: any(f["id"] == limit + 2 and f["t"] == "end" for f in link.frames)
        )
        assert next(f for f in link.frames if f["id"] == limit + 2)["status"] == 404
    finally:
        approval.set()
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)
    assert not channel._requests and not channel._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["return", "raise", "cancel"])
async def test_retiring_a_full_upload_queue_unblocks_the_channel(monkeypatch, outcome):
    import asyncio
    import base64

    from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream

    from kiro_crew.remote_session import git_gateway
    from kiro_crew.testing.wait import async_wait_until

    link = FrameLink()
    release = asyncio.Event()
    started = asyncio.Event()

    async def credential(host):
        return HEADER

    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:retire",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *args: True,
        http_session=HttpBackendUpstream(Path("unused"), status=404),
    )
    real_forward = channel._forward

    async def forward(rid, *args):
        if rid != 1:
            return await real_forward(rid, *args)
        started.set()
        await release.wait()
        if outcome == "raise":
            raise ConnectionError("upstream disconnected")
        if outcome == "cancel":
            raise asyncio.CancelledError()

    monkeypatch.setattr(channel, "_forward", forward)
    serve = asyncio.create_task(channel.serve())
    try:
        link.guest(
            {
                "t": "req",
                "id": 1,
                "method": "POST",
                "path": "/github.com/a/r/git-upload-pack",
                "headers": {},
            }
        )
        await asyncio.wait_for(started.wait(), 30)
        queue = channel._requests[1]
        for _ in range(queue.maxsize + 2):
            link.guest({"t": "body", "id": 1, "b": base64.b64encode(b"x").decode()})
        await async_wait_until(queue.full)
        release.set()
        link.get(2, "/github.com/a/r/info/refs?service=git-upload-pack")
        await async_wait_until(lambda: any(f["id"] == 2 and f["t"] == "end" for f in link.frames))
        assert next(f for f in link.frames if f["id"] == 2)["status"] == 404
        assert 1 not in channel._requests
    finally:
        release.set()
        link.stdout.feed_eof()
        await asyncio.wait_for(serve, 30)
    assert not channel._requests


def test_guest_retirement_releases_a_blocked_frame_reader(monkeypatch):
    import io
    import json
    import queue
    import threading
    from types import SimpleNamespace

    from kiro_crew.remote_session import git_proxy

    blocked = threading.Event()

    class ObservedQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            if self.full():
                blocked.set()
            return super().put(item, *args, **kwargs)

    monkeypatch.setattr(
        git_proxy, "queue", SimpleNamespace(Queue=ObservedQueue, Empty=queue.Empty, Full=queue.Full)
    )
    gateway = git_proxy.Gateway(out=io.StringIO())
    first, full = gateway.open()
    second, next_queue = gateway.open()
    for _ in range(full.maxsize):
        full.put({"t": "body"})
    gateway._in = io.StringIO(
        "\n".join(
            json.dumps(f)
            for f in (
                {"id": first, "t": "body"},
                {"id": first, "t": "end"},
                {"id": second, "t": "resp", "status": 200},
            )
        )
    )
    thread = threading.Thread(target=gateway.read_forever, daemon=True)
    thread.start()
    try:
        assert blocked.wait(30), "reader did not reach the full queue"
        gateway.done(first)
        thread.join(timeout=30)
        assert not thread.is_alive(), "retired queue stranded the shared reader"
        assert next_queue.get_nowait()["status"] == 200
        assert first not in gateway._queues
    finally:
        gateway.done(first)
        gateway.done(second)
        while True:
            try:
                full.get_nowait()
            except queue.Empty:
                break
        thread.join(timeout=30)


def test_guest_admission_is_bounded_and_reusable():
    import io

    from kiro_crew.remote_session import git_proxy

    gateway = git_proxy.Gateway(out=io.StringIO())
    ids = [gateway.open()[0] for _ in range(git_proxy.MAX_LIVE_REQUESTS)]
    with pytest.raises(OverflowError):
        gateway.open()
    assert len(gateway._queues) == git_proxy.MAX_LIVE_REQUESTS
    gateway.done(ids[0])
    assert gateway.open()[0] > ids[-1]
    assert len(gateway._queues) == git_proxy.MAX_LIVE_REQUESTS


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("final_status", [200, 401, 404])
async def test_fetch_retries_anonymously_and_preserves_body_and_status(method, final_status):
    import asyncio

    from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream, _Response

    from kiro_crew.remote_session import git_gateway

    class Upstream(HttpBackendUpstream):
        async def answer(self, method, url, headers, data, allow_redirects):
            body = b""
            if data is not None:
                async for part in data:
                    body += part
            self.calls.append((dict(headers), body))
            return _Response(401 if len(self.calls) == 1 else final_status, {}, b"upstream result")

    async def credential(host):
        return HEADER

    upstream, link = Upstream(Path("unused")), FrameLink()
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:anonymous",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *args: True,
        http_session=upstream,
    )
    body = asyncio.Queue()
    body.put_nowait(b"fetch negotiation")
    body.put_nowait(None)
    path = "/github.com/a/r/" + (
        "git-upload-pack" if method == "POST" else "info/refs?service=git-upload-pack"
    )
    await asyncio.wait_for(channel._handle(1, {"method": method, "path": path}, body), 30)
    assert len(upstream.calls) == 2
    assert upstream.calls[0][0]["Authorization"] == HEADER
    assert "Authorization" not in upstream.calls[1][0]
    assert (
        upstream.calls[0][1]
        == upstream.calls[1][1]
        == (b"fetch negotiation" if method == "POST" else b"")
    )
    assert link.frames[0]["t"] == "resp" and link.frames[0]["status"] == final_status
    assert link.frames[-1]["t"] == "end"
    assert not channel._credentials


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_push_authentication_never_falls_back_to_anonymous(missing):
    import asyncio

    from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream

    from kiro_crew.remote_session import git_gateway

    async def credential(host):
        if missing:
            raise git_gateway.GitProxyRefusal(502, "Kiro Crew: no login; gh auth login")
        return HEADER

    async def approve(*args):
        return True

    upstream, link = HttpBackendUpstream(Path("unused"), status=401), FrameLink()
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:push-only",
        approve=approve,
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *args: True,
        http_session=upstream,
    )
    body = asyncio.Queue()
    body.put_nowait(None)
    await await_until(
        asyncio.create_task(
            channel._handle(
                1,
                {"method": "GET", "path": "/github.com/a/r/info/refs?service=git-receive-pack"},
                body,
            )
        ),
        what="push authentication refusal",
    )
    assert len(upstream.calls) == (0 if missing else 1)
    assert link.frames[0]["t"] == "err" and "gh auth login" in link.frames[0]["message"]


@pytest.mark.asyncio
async def test_fetch_retry_buffer_has_a_hard_bound(monkeypatch):
    import asyncio

    from test_remote_git_proxy import HEADER, FrameLink, HttpBackendUpstream

    async def credential(host):
        return HEADER

    monkeypatch.setattr(git_gateway, "_MAX_FETCH_BODY", 3)
    upstream, link = HttpBackendUpstream(Path("unused"), status=200), FrameLink()
    channel = git_gateway.GitProxyChannel(
        link,
        "dashboard:body-bound",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda *args: True,
        http_session=upstream,
    )
    body = asyncio.Queue()
    body.put_nowait(b"four")
    body.put_nowait(None)
    await asyncio.wait_for(
        channel._handle(1, {"method": "POST", "path": "/github.com/a/r/git-upload-pack"}, body), 30
    )
    assert link.frames[0]["status"] == 413 and link.frames[0]["t"] == "err"
    assert not upstream.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["proxy", "workspace"])
async def test_capability_bootstrap_failure_closes_link_and_stops_worker(
    tmp_path, monkeypatch, component
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from test_remote_session_launch import _launch

    from kiro_crew.remote_session import relay

    launch = _launch(tmp_path, "bootstrap-failure")
    link = SimpleNamespace(
        start=Mock(), write=AsyncMock(side_effect=ConnectionError("closed")), aclose=AsyncMock()
    )
    stop = AsyncMock()
    monkeypatch.setattr(launch, "stop_worker", stop)
    monkeypatch.setattr(launch, "start_worker", AsyncMock(return_value=1))
    if component == "proxy":
        with pytest.raises(ConnectionError, match="closed"):
            await launch.start_git_proxy(
                "dashboard:bootstrap", agent="researcher", link_factory=lambda *a, **k: link
            )
    else:
        monkeypatch.setattr(launch, "install", AsyncMock())
        monkeypatch.setattr(launch, "reap_stale_workers", AsyncMock())
        monkeypatch.setattr(relay.WorkspaceRelay, "start", AsyncMock(return_value=1))
        monkeypatch.setattr(
            relay.WorkspaceRelay, "_open_link", AsyncMock(return_value=(link, "guest-worker"))
        )
        await launch.workspace_server(str(tmp_path))
        with pytest.raises(ConnectionError, match="closed"):
            await launch.relay._open_link()
    link.aclose.assert_awaited_once()
    stop.assert_awaited_once()
    link.write.assert_awaited_once_with(launch.git_proxy_secret.encode("ascii") + b"\n")
    assert launch.git_proxy is None
