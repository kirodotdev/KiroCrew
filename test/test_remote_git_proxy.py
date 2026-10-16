"""Remote git proxy: git runs in the guest, the credential stays on the gateway.

The end-to-end cases run a REAL ``git`` client against the REAL guest proxy
(``git_proxy.py`` as a child process) and the REAL gateway channel. Only the
upstream is faked: ``git http-backend`` over local bare repositories stands in
for each git host, so every byte crosses the actual framing and nothing
reaches the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.remote_session import git_gateway, git_proxy, workspace_tools
from kiro_crew.remote_session.git_gateway import (
    HOSTS,
    GitProxyChannel,
    GitProxyRefusal,
    host_credential,
    parse_request,
)
from kiro_crew.testing.wait import async_wait_until, await_until

PROXY_SECRET = "a" * 64
TOKEN = "gho_" + "t" * 36
HEADER = "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is required")


@pytest.fixture(autouse=True)
def _fresh_push_approvals(_floor_monkeypatch):
    _floor_monkeypatch.setattr(git_gateway, "_PUSH_APPROVED", {})


# ── Request validation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "service", "upstream"),
    [
        (
            "GET",
            "/github.com/acme/widgets.git/info/refs?service=git-upload-pack",
            "git-upload-pack",
            "https://github.com/acme/widgets.git/info/refs?service=git-upload-pack",
        ),
        (
            "POST",
            "/github.com/a-b/c.d_e.git/git-receive-pack",
            "git-receive-pack",
            "https://github.com/a-b/c.d_e.git/git-receive-pack",
        ),
        (
            # Nested groups, and GitLab gets .git so it does not redirect.
            "GET",
            "/gitlab.com/group/sub/deeper/proj/info/refs?service=git-upload-pack",
            "git-upload-pack",
            "https://gitlab.com/group/sub/deeper/proj.git/info/refs?service=git-upload-pack",
        ),
        (
            "POST",
            "/bitbucket.org/team/repo.git/git-upload-pack",
            "git-upload-pack",
            "https://bitbucket.org/team/repo.git/git-upload-pack",
        ),
        (
            "GET",
            "/dev.azure.com/contoso/My%20Project/_git/web-app/info/refs?service=git-receive-pack",
            "git-receive-pack",
            "https://dev.azure.com/contoso/My%20Project/_git/web-app/info/refs"
            "?service=git-receive-pack",
        ),
        (
            "POST",
            "/dev.azure.com/contoso/_git/contoso/git-upload-pack",
            "git-upload-pack",
            "https://dev.azure.com/contoso/_git/contoso/git-upload-pack",
        ),
    ],
)
def test_git_smart_http_requests_are_accepted_per_host(method, path, service, upstream):
    request = parse_request(method, path)
    assert request.service == service
    assert request.upstream_url() == upstream


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/evil.example/acme/widgets.git/info/refs?service=git-upload-pack"),
        ("GET", "/github.com/acme/widgets.git/info/refs"),
        ("GET", "/github.com/acme/widgets.git/info/refs?service=git-upload-pack&x=1"),
        ("GET", "/github.com/acme/widgets.git/HEAD"),
        ("GET", "/github.com/acme/widgets.git/git-upload-pack"),
        ("POST", "/github.com/acme/widgets.git/info/refs?service=git-upload-pack"),
        ("POST", "/github.com/acme/widgets.git/git-upload-pack?x=1"),
        ("GET", "/github.com/acme/../info/refs?service=git-upload-pack"),
        ("GET", "/github.com/acme/./info/refs?service=git-upload-pack"),
        ("GET", "/github.com/-acme/widgets/info/refs?service=git-upload-pack"),
        ("GET", "/github.com/acme/sub/widgets/info/refs?service=git-upload-pack"),
        ("GET", "/github.com@evil.example/acme/widgets/info/refs?service=git-upload-pack"),
        ("GET", "http://evil.example/acme/widgets/info/refs?service=git-upload-pack"),
        # GitLab routes live under "/-/"; a project path never contains it.
        ("GET", "/gitlab.com/group/-/proj/info/refs?service=git-upload-pack"),
        ("GET", "/gitlab.com/proj/info/refs?service=git-upload-pack"),
        ("GET", "/bitbucket.org/team/sub/repo/info/refs?service=git-upload-pack"),
        ("GET", "/dev.azure.com/contoso/proj/repo/info/refs?service=git-upload-pack"),
        # Percent-encoding cannot smuggle a separator or a traversal.
        ("GET", "/dev.azure.com/contoso/a%2Fb/_git/r/info/refs?service=git-upload-pack"),
        ("GET", "/dev.azure.com/contoso/%2E%2E/_git/r/info/refs?service=git-upload-pack"),
        ("GET", "/dev.azure.com/contoso/p/_git/r%40evil/info/refs?service=git-upload-pack"),
        ("GET", "/dev.azure.com/contoso/p/_git/r%0A/info/refs?service=git-upload-pack"),
    ],
)
def test_anything_else_is_refused(method, path):
    with pytest.raises(GitProxyRefusal) as refused:
        parse_request(method, path)
    assert refused.value.status == 403


def test_the_repository_named_in_an_approval_includes_its_host():
    request = parse_request("POST", "/gitlab.com/group/sub/proj.git/git-receive-pack")
    assert request.repository == "gitlab.com/group/sub/proj"
    assert request.is_push


def test_the_guest_rewrites_cover_exactly_the_hosts_the_gateway_serves():
    # workspace_tools runs in the guest and cannot import git_gateway; pin them equal.
    assert set(workspace_tools.GIT_PROXY_REWRITES) == set(HOSTS)
    for host, spec in HOSTS.items():
        assert tuple(workspace_tools.GIT_PROXY_REWRITES[host]) == spec.rewrites


def test_commands_are_routed_through_the_proxy_only_when_it_runs(tmp_path):
    import socket

    port_file = tmp_path / "git-proxy.port"
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        port_file.write_text(f"{port}\n")
        env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ASKPASS"] == ""
    pairs = {
        env[f"GIT_CONFIG_VALUE_{i}"]: env[f"GIT_CONFIG_KEY_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["git@gitlab.com:"] == f"url.http://127.0.0.1:{port}/gitlab.com/.insteadOf"
    assert pairs["https://dev.azure.com/"] == (
        f"url.http://127.0.0.1:{port}/dev.azure.com/.insteadOf"
    )
    assert (
        pairs[f"Authorization: Bearer {PROXY_SECRET}"]
        == f"http.http://127.0.0.1:{port}/.extraHeader"
    )
    assert len(pairs) == 1 + sum(len(spec.rewrites) for spec in HOSTS.values())
    # Nothing listening any more: a stale file must not break every clone.
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}


# ── Credential sources on the gateway machine ────────────────────────────


@pytest.fixture
def clis(tmp_path, monkeypatch):
    """Stub the native subprocess seam; assert argv, stdin and stream routing."""
    planted: dict[str, str] = {}
    calls: list[dict] = []
    answers: dict[str, object] = {}
    monkeypatch.setattr(git_gateway, "_binary", lambda name: planted.get(name))
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: planted.get("git"))

    class Process:
        def __init__(self, answer):
            self.returncode, self.output = answer

        async def communicate(self, data):
            calls[-1]["stdin"] = data
            return self.output, None

        async def wait(self):
            return self.returncode

    async def spawn(binary, *argv, **kwargs):
        assert binary in answers, "unexpected executable"
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        call = {"name": Path(binary).stem, "argv": argv, **kwargs}
        calls.append(call)
        answer = answers[binary]
        return Process(answer(call))

    monkeypatch.setattr(git_gateway.asyncio, "create_subprocess_exec", spawn)

    def plant(name, answer):
        binary = str(tmp_path / f"{name}.exe")
        planted[name] = binary
        answers[binary] = answer
        return calls

    return plant


def _decoded(header: str) -> str:
    kind, _, value = header.partition(" ")
    return f"{kind} {base64.b64decode(value).decode()}" if kind == "Basic" else header


@pytest.mark.asyncio
async def test_each_host_reads_its_own_cli_in_its_own_auth_format(clis):
    calls = clis("gh", lambda _: (0, TOKEN.encode()))
    clis("glab", lambda _: (0, b"glpat-abc"))
    clis("az", lambda _: (0, b"eyJ.az"))
    assert _decoded(await host_credential("github.com")) == f"Basic x-access-token:{TOKEN}"
    assert _decoded(await host_credential("gitlab.com")) == "Basic oauth2:glpat-abc"
    assert await host_credential("dev.azure.com") == "Bearer eyJ.az"
    assert [c["argv"] for c in calls] == [
        ("auth", "token", "--hostname", "github.com"),
        ("config", "get", "token", "--host", "gitlab.com"),
        (
            "account",
            "get-access-token",
            "--resource",
            "499b84ac-1321-427f-aa17-267ca6975798",
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ),
    ]
    assert all(c["stdin"] == b"" and c["stdout"] == asyncio.subprocess.PIPE for c in calls)


@pytest.mark.asyncio
async def test_git_credential_helper_answers_bitbucket_and_backs_up_the_clis(clis):
    answers = iter(
        [(0, b"username=kyle\npassword=app-pw\n"), (0, b"username=me\npassword=glpat-helper\n")]
    )
    calls = clis("git", lambda _: next(answers))
    assert _decoded(await host_credential("bitbucket.org")) == "Basic kyle:app-pw"
    assert _decoded(await host_credential("gitlab.com")) == "Basic me:glpat-helper"
    assert [c["argv"] for c in calls] == [("credential", "fill")] * 2
    assert [c["stdin"] for c in calls] == [
        b"protocol=https\nhost=bitbucket.org\n\n",
        b"protocol=https\nhost=gitlab.com\n\n",
    ]


@pytest.mark.asyncio
async def test_no_login_anywhere_is_a_clear_refusal_naming_the_fix(clis):
    clis("gh", lambda _: (1, b""))
    with pytest.raises(GitProxyRefusal, match="gh auth login"):
        await host_credential("github.com")
    with pytest.raises(GitProxyRefusal, match="app password"):
        await host_credential("bitbucket.org")
    with pytest.raises(GitProxyRefusal, match="az login"):
        await host_credential("dev.azure.com")


@pytest.mark.asyncio
async def test_the_git_helper_is_never_allowed_to_prompt(clis):
    calls = clis("git", lambda _: (0, b"username=u\npassword=p\n"))
    await host_credential("bitbucket.org")
    env = calls[0]["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_ASKPASS"] == "" and env["SSH_ASKPASS"] == ""


# ── End to end: real git, real guest proxy, real gateway channel ─────────


class ProxyLink:
    """The guest proxy's stdio, standing in for the authenticated wire link."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        assert process.stdout is not None
        self.stdout = process.stdout
        self.sent = bytearray()

    async def write(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.sent.extend(data)
        self.process.stdin.write(data)
        await self.process.stdin.drain()


class _Content:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._data), size):
            yield self._data[start : start + size]


class _Response:
    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.content = _Content(body)


class HttpBackendUpstream:
    """``git http-backend`` over ``root/<host>/...`` bare repos, answering as each host."""

    def __init__(self, root: Path, *, status: int | None = None) -> None:
        self.root = root
        self.status = status
        self.calls: list[dict] = []

    def request(self, method, url, *, headers, data, allow_redirects):
        return _Pending(self, method, url, headers, data, allow_redirects)

    async def close(self) -> None:
        pass

    async def answer(self, method, url, headers, data, allow_redirects) -> _Response:
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "redirects": allow_redirects}
        )
        if self.status is not None:
            return _Response(self.status, {}, b"")
        assert url.startswith("https://")
        host, _, rest = url[len("https://") :].partition("/")
        assert host in HOSTS
        path, _, query = ("/" + rest).partition("?")
        body = b""
        if data is not None:
            async for piece in data:
                body += piece
        env = {
            "PATH": os.path.dirname(GIT) + os.pathsep + os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(self.root / host),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "proxy",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.root),
        }
        for name in ("content-encoding", "git-protocol"):
            if name in headers:
                env["HTTP_" + name.upper().replace("-", "_")] = headers[name]
        process = await asyncio.create_subprocess_exec(
            GIT,
            "http-backend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self.root),
        )
        out, _ = await asyncio.wait_for(process.communicate(body), 60)
        head, _, payload = out.partition(b"\r\n\r\n")
        status, response_headers = 200, {}
        for line in head.decode("latin-1").split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            elif key:
                response_headers[key] = value.strip()
        return _Response(status, response_headers, payload)


class _Pending:
    def __init__(self, upstream, *args) -> None:
        self._upstream, self._args = upstream, args

    async def __aenter__(self):
        return await self._upstream.answer(*self._args)

    async def __aexit__(self, *exc):
        return False


def _git(*args, cwd, env=None, check=True):
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=check,
    )


def _client_env(tmp_path: Path, port_file: Path) -> dict:
    home = tmp_path / "client-home"
    home.mkdir(exist_ok=True)
    return {
        # HOME and PATH as a pair: git is resolved absolutely, never via a shim.
        "HOME": str(home),
        "PATH": os.path.dirname(GIT) + os.pathsep + "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        **workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET),
    }


def _seed_bare(tmp_path: Path, bare: Path) -> Path:
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("config", "http.receivepack", "true", cwd=bare)
    seed = tmp_path / f"seed-{abs(hash(str(bare)))}"
    _git("init", "-b", "main", str(seed), cwd=tmp_path)
    (seed / "README").write_text("hello\n")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    for args in (
        ("add", "README"),
        ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "seed"),
        ("push", str(bare), "main"),
    ):
        _git(*args, cwd=seed, env=env)
    return bare


@pytest.fixture
def upstream_repo(tmp_path):
    """Bare repos where each host would keep them, one commit each."""
    root = tmp_path / "upstream"
    bares = {
        "github": _seed_bare(tmp_path, root / "github.com" / "acme" / "widgets.git"),
        "gitlab": _seed_bare(tmp_path, root / "gitlab.com" / "group" / "sub" / "proj.git"),
        "bitbucket": _seed_bare(tmp_path, root / "bitbucket.org" / "team" / "repo.git"),
        "azure": _seed_bare(tmp_path, root / "dev.azure.com" / "contoso" / "web" / "_git" / "app"),
    }
    return root, bares


async def _run_channel(tmp_path, upstream, *, approve=None, credential_source=None):
    port_file = tmp_path / "git-proxy.port"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        git_proxy.__file__,
        "--port-file",
        str(port_file),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    serve = None
    try:
        assert process.stdin is not None
        process.stdin.write(PROXY_SECRET.encode() + b"\n")
        await process.stdin.drain()
        link = ProxyLink(process)
        approvals: list[tuple[str, str]] = []

        async def approve_push(session_key, repository):
            approvals.append((session_key, repository))
            return True

        async def credential(host):
            return HEADER

        channel = GitProxyChannel(
            link,
            "dashboard:chat-git",
            approve=approve or approve_push,
            credential_source=credential_source or credential,
            agent="researcher",
            egress_permitted=lambda key, host, agent: True,
            http_session=upstream,
        )
        serve = asyncio.ensure_future(channel.serve())
        await async_wait_until(
            port_file.exists, off_loop=True, describe=lambda: "guest proxy port is unpublished"
        )
        return process, serve, port_file, link, approvals
    except BaseException:
        try:
            if process.returncode is None:
                process.kill()
            await process.wait()
        finally:
            if serve is not None:
                serve.cancel()
                await asyncio.gather(serve, return_exceptions=True)
        raise


async def _stop(process, serve):
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
    try:
        await await_until(asyncio.create_task(process.wait()), what="guest proxy exit")
    except AssertionError:
        process.kill()
        await process.wait()
    await await_until(asyncio.gather(serve, return_exceptions=True), what="gateway channel closed")


async def _in_thread(fn, *args, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), 90)


@pytest.mark.asyncio
async def test_clone_and_push_go_through_the_gateway_with_its_credential(tmp_path, upstream_repo):
    root, bares = upstream_repo
    upstream = HttpBackendUpstream(root)
    process, serve, port_file, link, approvals = await _run_channel(tmp_path, upstream)
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        result = await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        assert (clone / "README").read_text() == "hello\n", result.stderr
        assert approvals == []  # a clone asks no one

        (clone / "NEW").write_text("from the guest\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "guest change", cwd=clone, env=env)
        pushed = await _in_thread(_git, "push", "origin", "main", cwd=clone, env=env, check=False)
        assert pushed.returncode == 0, pushed.stderr
        bare = bares["github"]
        assert _git("log", "-1", "--format=%s", "main", cwd=bare).stdout.strip() == "guest change"
        # Asked once for the repository, though a push is two requests.
        assert approvals == [("dashboard:chat-git", "github.com/acme/widgets")]

        assert upstream.calls and all(
            c["headers"]["Authorization"] == HEADER for c in upstream.calls
        )
        assert all(
            c["url"].startswith("https://github.com/acme/widgets.git/") for c in upstream.calls
        )
        assert all(c["redirects"] is False for c in upstream.calls)
        # The credential never crossed to the guest, in any form.
        sent = bytes(link.sent)
        assert TOKEN.encode() not in sent and HEADER.encode() not in sent
    finally:
        await _stop(process, serve)
    assert not port_file.exists()  # the proxy cleans up its own port file


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_upstream", "host"),
    [
        # SSH spelling, nested groups, no .git: rewritten, and .git added upstream.
        (
            "git@gitlab.com:group/sub/proj",
            "https://gitlab.com/group/sub/proj.git/",
            "gitlab.com",
        ),
        ("https://bitbucket.org/team/repo.git", "https://bitbucket.org/team/repo.git/", None),
        (
            "https://dev.azure.com/contoso/web/_git/app",
            "https://dev.azure.com/contoso/web/_git/app/",
            None,
        ),
    ],
)
async def test_gitlab_bitbucket_and_azure_clone_through_the_gateway(
    tmp_path, upstream_repo, url, expected_upstream, host
):
    root, _bares = upstream_repo
    upstream = HttpBackendUpstream(root)
    asked: list[str] = []

    async def credential(requested_host):
        asked.append(requested_host)
        return HEADER

    process, serve, port_file, _link, _ = await _run_channel(
        tmp_path, upstream, credential_source=credential
    )
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        result = await _in_thread(
            _git, "clone", url, str(clone), cwd=tmp_path, env=env, check=False
        )
        assert result.returncode == 0, result.stderr
        assert (clone / "README").read_text() == "hello\n"
        assert upstream.calls and all(
            c["url"].startswith(expected_upstream) for c in upstream.calls
        )
        assert set(asked) == {expected_upstream.split("/")[2]}
    finally:
        await _stop(process, serve)


@pytest.mark.asyncio
async def test_a_refused_push_reaches_git_as_a_failure_and_changes_nothing(tmp_path, upstream_repo):
    root, bares = upstream_repo
    bare = bares["github"]
    before = _git("rev-parse", "main", cwd=bare).stdout

    async def refuse(session_key, repository):
        return False

    process, serve, port_file, _link, _ = await _run_channel(
        tmp_path, HttpBackendUpstream(root), approve=refuse
    )
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        (clone / "NEW").write_text("x\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "nope", cwd=clone, env=env)
        pushed = await _in_thread(_git, "push", "origin", "main", cwd=clone, env=env, check=False)
        assert pushed.returncode != 0
        assert "not approved" in pushed.stderr
    finally:
        await _stop(process, serve)
    assert _git("rev-parse", "main", cwd=bare).stdout == before


@pytest.mark.asyncio
async def test_a_rejected_credential_never_makes_git_prompt(tmp_path, upstream_repo):
    root, _bares = upstream_repo
    process, serve, port_file, _link, _ = await _run_channel(
        tmp_path, HttpBackendUpstream(root, status=401)
    )
    try:
        env = _client_env(tmp_path, port_file)
        result = await _in_thread(
            _git,
            "clone",
            "https://gitlab.com/group/sub/proj.git",
            str(tmp_path / "c"),
            cwd=tmp_path,
            env=env,
            check=False,
        )
        assert result.returncode != 0
        assert "terminal prompts disabled" in result.stderr or "401" in result.stderr
        assert "terminal prompts disabled" in result.stderr
    finally:
        await _stop(process, serve)


@pytest.mark.asyncio
async def test_a_missing_login_allows_an_anonymous_public_clone(tmp_path, upstream_repo):
    root, _bares = upstream_repo

    async def signed_out(host):
        raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")

    process, serve, port_file, _link, _ = await _run_channel(
        tmp_path, HttpBackendUpstream(root), credential_source=signed_out
    )
    try:
        env = _client_env(tmp_path, port_file)
        result = await _in_thread(
            _git,
            "clone",
            "https://bitbucket.org/team/repo.git",
            str(tmp_path / "c"),
            cwd=tmp_path,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "c" / "README").read_text() == "hello\n"
    finally:
        await _stop(process, serve)


# ── Channel units ────────────────────────────────────────────────────────


class FrameLink:
    """A link whose guest side is driven directly by the test."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.frames: list[dict] = []

    async def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            self.frames.append(json.loads(line))

    def guest(self, frame: dict) -> None:
        self.stdout.feed_data((json.dumps(frame) + "\n").encode())

    def get(self, rid: int, path: str) -> None:
        self.guest({"t": "req", "id": rid, "method": "GET", "path": path, "headers": {}})
        self.guest({"t": "end", "id": rid})


@pytest.mark.asyncio
async def test_egress_is_judged_per_host_before_any_credential_is_read():
    link, reads, judged = FrameLink(), [], []

    async def credential(host):
        reads.append(host)
        return HEADER

    def egress(key, host, agent):
        judged.append(host)
        return False

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=egress,
        http_session=HttpBackendUpstream(Path("/nonexistent")),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.get(1, "/dev.azure.com/contoso/_git/r/info/refs?service=git-upload-pack")
        await async_wait_until(lambda: link.frames, describe=lambda: "egress refusal frame")
        assert link.frames[0]["t"] == "err" and link.frames[0]["status"] == 403
        assert "dev.azure.com" in link.frames[0]["message"]
        assert judged == ["dev.azure.com"] and reads == []
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_credentials_are_cached_per_host_and_a_redirect_is_not_followed():
    link, reads = FrameLink(), []

    async def credential(host):
        reads.append(host)
        return HEADER

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        http_session=HttpBackendUpstream(Path("/nonexistent"), status=302),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.get(1, "/github.com/acme/w/info/refs?service=git-upload-pack")
        link.get(2, "/github.com/acme/w/info/refs?service=git-upload-pack")
        link.get(3, "/bitbucket.org/team/repo/info/refs?service=git-upload-pack")
        await async_wait_until(
            lambda: len(link.frames) == 3, describe=lambda: f"{len(link.frames)} redirect refusals"
        )
        assert {f["status"] for f in link.frames} == {502}
        assert all("redirect" in f["message"] for f in link.frames)
        assert sorted(reads) == ["bitbucket.org", "github.com"]
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_push_approval_fails_closed_without_a_dashboard(monkeypatch):
    import kiro_crew.slack.handler as handler

    monkeypatch.setattr(handler, "_dashboard_state", None)
    assert await git_gateway.dashboard_push_approval("dashboard:x", "github.com/acme/w") is False


@pytest.mark.asyncio
async def test_login_status_reads_exit_codes_and_never_asks_the_credential_helper(clis):
    calls = clis("gh", lambda _: (0, b""))
    clis("az", lambda _: (1, b""))

    def unexpected_helper(_):
        raise AssertionError("readiness must not read a credential")

    clis("git", unexpected_helper)
    assert await git_gateway.login_status("github.com") == "signed_in"
    assert await git_gateway.login_status("dev.azure.com") == "unchecked"
    assert await git_gateway.login_status("bitbucket.org") == "unchecked"
    assert [c["argv"] for c in calls] == [
        ("auth", "status", "--hostname", "github.com"),
        ("account", "show", "--output", "none"),
    ]
    assert all(c["stdout"] == asyncio.subprocess.DEVNULL for c in calls)
    assert all(c["stdin"] == asyncio.subprocess.DEVNULL for c in calls)
    clis("gh", lambda _: (1, b""))
    assert await git_gateway.login_status("github.com") == "not_signed_in"
