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
from urllib.parse import urlsplit

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

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
#: The guest proxy runs inside the remote workspace, which is always POSIX; these
#: tests drive it end to end, so they do not run on a Windows host.
_POSIX_GUEST = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")


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


def test_a_push_request_names_its_repository_with_its_host():
    request = parse_request("POST", "/gitlab.com/group/sub/proj.git/git-receive-pack")
    assert request.repository == "gitlab.com/group/sub/proj"
    assert request.is_push


def test_the_guest_rewrites_cover_exactly_the_hosts_the_gateway_serves():
    # workspace_tools runs in the guest and cannot import git_gateway; pin them equal.
    assert set(workspace_tools.GIT_PROXY_REWRITES) == set(HOSTS)
    for prefixes in workspace_tools.GIT_PROXY_REWRITES.values():
        assert prefixes and all(p.endswith(("/", ":")) for p in prefixes)


def test_commands_are_routed_through_the_proxy_only_when_it_runs(tmp_path, monkeypatch):
    import socket

    # The wait for a first answer has its own test below.
    monkeypatch.setattr(workspace_tools, "GIT_PROXY_FIRST_LEND_WAIT_SECS", 0.0)
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        port_file.write_text(f"{port}\n")
        # No lendable list yet, or another proxy's: route nothing.
        assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
        lend.write_text(f"{port + 1} {' '.join(HOSTS)}\n")
        assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
        lend.write_text(f"{port}\n")  # strict, or no login anywhere
        assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
        lend.write_text(f"{port} {' '.join(HOSTS)} evil.example\n")
        env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    # The workspace's own askpass and terminal prompt are left alone.
    assert "GIT_ASKPASS" not in env and "GIT_TERMINAL_PROMPT" not in env
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
    assert pairs["!f() { echo quit=1; }; f"] == f"credential.http://127.0.0.1:{port}/.helper"
    assert len(pairs) == 2 + sum(len(p) for p in workspace_tools.GIT_PROXY_REWRITES.values())
    assert not any("evil.example" in key for key in pairs.values())
    # Nothing listening any more: a stale file must not break every clone.
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}


def test_a_command_before_the_first_answer_waits_for_it(tmp_path, monkeypatch):
    """A first private clone right after the session starts still gets the login."""
    # A fake clock: each poll's sleep advances it, and the gateway's answer can
    # be made to land at a chosen poll, so nothing here depends on real time.
    # Whether the proxy still answers is faked too (its own test is above).
    now = [0.0]
    sleeps: list[float] = []
    on_poll: dict[int, object] = {}
    alive = [True]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        action = on_poll.get(len(sleeps))
        if callable(action):
            action()

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: now[0])
    monkeypatch.setattr(workspace_tools, "_sleep", sleep)
    monkeypatch.setattr(workspace_tools, "_listening", lambda _port: alive[0])
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    port = 40000
    port_file.write_text(f"{port}\n")
    # The gateway's answer lands while the command is waiting (third poll).
    on_poll[3] = lambda: lend.write_text(f"{port} github.com\n")
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert len(sleeps) == 3
    assert any(
        v == "https://github.com/" for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")
    )
    # A published empty list (strict, or no login anywhere) is used at once.
    sleeps.clear()
    on_poll.clear()
    lend.write_text(f"{port}\n")
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    assert sleeps == []
    # No answer within the bound: the command runs on the workspace's own login.
    lend.unlink()
    start = now[0]
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    assert now[0] - start >= workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS
    # One poll every 0.1 s of the bound (float rounding may add one).
    assert abs(len(sleeps) - round(workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS / 0.1)) <= 1
    # A proxy that stops while the command waits ends the wait at that poll.
    sleeps.clear()
    on_poll[2] = lambda: alive.__setitem__(0, False)
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    assert len(sleeps) == 2


def test_the_proxy_appends_to_git_config_the_command_already_sets(tmp_path):
    """A workspace that sets its own GIT_CONFIG_* entries keeps them."""
    import socket

    port_file = tmp_path / "git-proxy.port"
    base = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "dev",
        "GIT_CONFIG_KEY_1": "core.editor",
        "GIT_CONFIG_VALUE_1": "vi",
    }
    base.update(git_maintenance_off_entries(base))
    own = int(base["GIT_CONFIG_COUNT"])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port_file.write_text(f"{listener.getsockname()[1]}\n")
        lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
        lend.write_text(f"{listener.getsockname()[1]} github.com\n")
        env = {**base, **workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET, base)}
        plain = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    added = int(plain["GIT_CONFIG_COUNT"])
    assert env["GIT_CONFIG_COUNT"] == str(own + added)
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("user.name", "dev")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("core.editor", "vi")
    assert [env[f"GIT_CONFIG_KEY_{i}"] for i in range(own, own + added)] == [
        plain[f"GIT_CONFIG_KEY_{i}"] for i in range(added)
    ]


# ── Credential sources on the gateway machine ────────────────────────────


@pytest.fixture
def clis(tmp_path, monkeypatch):
    """Stub the native subprocess seam; assert argv, stdin and stream routing."""
    planted: dict[str, str] = {}
    calls: list[dict] = []
    answers: dict[str, object] = {}
    monkeypatch.setattr(git_gateway, "_binary", lambda name: planted.get(name))
    # The `git credential fill` path; the Windows direct path has its own tests
    # (test_remote_git_windows_lending).
    monkeypatch.setattr(git_gateway, "_runs_helpers_directly", lambda: False)
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: planted.get("git"))
    monkeypatch.setattr(
        "kiro_crew.github_runner.validate_provider_executable",
        lambda path, *, require_protected: path,
    )

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
        if argv[:2] == ("config", "--null"):
            return Process((0, b"credential.helper\nstore\0"))
        if argv == ("--exec-path",):
            return Process((0, os.fsencode(tmp_path)))
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
    fills = [c for c in calls if c["argv"][-2:] == ("credential", "fill")]
    assert len(fills) == 2
    assert all(c["argv"] == ("credential", "fill") for c in fills)
    assert all(
        c["env"]["GIT_CONFIG_KEY_0"] == "credential.helper" and c["env"]["GIT_CONFIG_VALUE_0"]
        for c in fills
    )
    assert [c["stdin"] for c in fills] == [
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


#: Anything NOT routed through the proxy must fail offline, never reach a forge:
#: SSH is a no-op program and HTTPS goes to a closed loopback port.
OFFLINE = {
    "GIT_SSH_COMMAND": "false",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "NO_PROXY": "127.0.0.1",
    "no_proxy": "127.0.0.1",
}


def _client_env(tmp_path: Path, port_file: Path, *, routed: bool = True) -> dict:
    home = tmp_path / "client-home"
    home.mkdir(exist_ok=True)
    proxy = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert bool(proxy) is routed, proxy
    return {
        **OFFLINE,
        # HOME and PATH as a pair: git is resolved absolutely, never via a shim.
        "HOME": str(home),
        "PATH": os.path.dirname(GIT) + os.pathsep + "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        **proxy,
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


async def _run_channel(tmp_path, upstream, *, credential_source=None):
    port_file = tmp_path / "git-proxy.port"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        git_proxy.__file__,
        "--port-file",
        str(port_file),
        "--build",
        git_proxy.build_digest(),
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

        async def credential(host):
            return HEADER

        channel = GitProxyChannel(
            link,
            "dashboard:chat-git",
            credential_source=credential_source or credential,
            agent="researcher",
            egress_permitted=lambda key, host, agent: True,
            http_session=upstream,
        )
        serve = asyncio.ensure_future(channel.serve())
        lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
        await async_wait_until(
            lambda: port_file.exists() and lend.exists(),
            off_loop=True,
            describe=lambda: "guest proxy port or lendable hosts unpublished",
        )
        return process, serve, port_file, link
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


@_POSIX_GUEST
@pytest.mark.asyncio
async def test_clone_and_push_go_through_the_gateway_with_its_credential(tmp_path, upstream_repo):
    root, bares = upstream_repo
    upstream = HttpBackendUpstream(root)
    process, serve, port_file, link = await _run_channel(tmp_path, upstream)
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        result = await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        assert (clone / "README").read_text() == "hello\n", result.stderr

        (clone / "NEW").write_text("from the guest\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "guest change", cwd=clone, env=env)
        # A feature branch goes straight through: no card, no prompt, as locally.
        pushed = await _in_thread(
            _git, "push", "origin", "HEAD:feat/guest", cwd=clone, env=env, check=False
        )
        assert pushed.returncode == 0, pushed.stderr
        bare = bares["github"]
        assert (
            _git("log", "-1", "--format=%s", "feat/guest", cwd=bare).stdout.strip()
            == "guest change"
        )

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


@_POSIX_GUEST
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

    process, serve, port_file, _link = await _run_channel(
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
        # Every host is asked once to publish what can be lent; only this one is used.
        assert set(asked) == set(HOSTS)
    finally:
        await _stop(process, serve)


def _refs(bare: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=bare).stdout


def _protected_names() -> list[str]:
    from kiro_crew.security import _PROTECTED_BRANCHES

    return sorted(_PROTECTED_BRANCHES)


@_POSIX_GUEST
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refspecs",
    [
        *[(f"HEAD:{name}",) for name in _protected_names()],
        ("HEAD:refs/heads/main",),
        ("+HEAD:main",),  # a force-push
        (":main",),  # a delete
        ("HEAD:MAIN",),  # the local check reads the command lower-cased too
        ("HEAD:refs/remotes/origin/main",),
        # One protected command refuses the whole push, the feature one too.
        ("HEAD:feat/ok", "HEAD:main"),
    ],
    ids=lambda specs: " ".join(specs),
)
async def test_a_push_to_a_protected_branch_is_refused_whole_and_changes_nothing(
    tmp_path, upstream_repo, refspecs
):
    root, bares = upstream_repo
    bare = bares["github"]
    before = _refs(bare)
    upstream = HttpBackendUpstream(root)
    process, serve, port_file, _link = await _run_channel(tmp_path, upstream)
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        (clone / "NEW").write_text("x\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "nope", cwd=clone, env=env)
        pushed = await _in_thread(
            _git, "push", "origin", *refspecs, cwd=clone, env=env, check=False
        )
        assert pushed.returncode != 0
        assert "remote: Kiro Crew: the push to github.com/acme/widgets was refused" in (
            pushed.stderr
        ), pushed.stderr
        assert "is a protected branch" in pushed.stderr
        assert "[remote rejected]" in pushed.stderr and "(protected branch)" in pushed.stderr
        # Refused before anything of the push went upstream.
        assert not [c for c in upstream.calls if c["url"].endswith("/git-receive-pack")]
    finally:
        await _stop(process, serve)
    assert _refs(bare) == before


@_POSIX_GUEST
@pytest.mark.asyncio
async def test_a_force_push_to_a_feature_branch_goes_through(tmp_path, upstream_repo):
    root, bares = upstream_repo
    bare = bares["github"]
    process, serve, port_file, _link = await _run_channel(tmp_path, HttpBackendUpstream(root))
    try:
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        (clone / "NEW").write_text("one\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "first", cwd=clone, env=env)
        await _in_thread(_git, "push", "origin", "HEAD:feat/x", cwd=clone, env=env)
        await _in_thread(_git, "commit", "--amend", "-m", "rewritten", cwd=clone, env=env)
        pushed = await _in_thread(
            _git, "push", "--force", "origin", "HEAD:feat/x", cwd=clone, env=env, check=False
        )
        assert pushed.returncode == 0, pushed.stderr
        deleted = await _in_thread(
            _git, "push", "origin", ":feat/x", cwd=clone, env=env, check=False
        )
        assert deleted.returncode == 0, deleted.stderr
    finally:
        await _stop(process, serve)
    assert "refs/heads/feat/x" not in _refs(bare)


@pytest.mark.asyncio
async def test_a_rejected_credential_never_makes_git_prompt(tmp_path, upstream_repo):
    """A routed request that ends in a 401 asks no one, though the workspace's
    own askpass helper stays in place for the hosts that are not routed."""
    root, _bares = upstream_repo
    process, serve, port_file, _link = await _run_channel(
        tmp_path, HttpBackendUpstream(root, status=401)
    )
    asked = tmp_path / "askpass-ran"
    askpass = tmp_path / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\necho ran >> {asked}\necho secret\n")
    askpass.chmod(0o755)
    try:
        env = {**_client_env(tmp_path, port_file), "GIT_ASKPASS": str(askpass)}
        assert "GIT_ASKPASS" not in workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
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
        assert not asked.exists(), "git asked the workspace's askpass for a routed request"
    finally:
        await _stop(process, serve)


@pytest.mark.asyncio
async def test_a_host_with_no_gateway_login_is_not_routed_at_all(tmp_path, upstream_repo):
    """With nothing to lend, git keeps the workspace's own login: no rewrite."""
    root, _bares = upstream_repo

    async def signed_out(host):
        raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")

    upstream = HttpBackendUpstream(root)
    process, serve, port_file, _link = await _run_channel(
        tmp_path, upstream, credential_source=signed_out
    )
    try:
        lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
        assert lend.read_text().split() == [port_file.read_text().strip()]
        env = _client_env(tmp_path, port_file, routed=False)
        assert "GIT_CONFIG_COUNT" not in env
        assert upstream.calls == []
    finally:
        await _stop(process, serve)


@_POSIX_GUEST
@pytest.mark.asyncio
async def test_only_the_hosts_the_gateway_can_lend_for_are_routed(tmp_path, upstream_repo):
    """GitHub lendable, the rest not: GitHub goes through the proxy (and its
    protected-branch rule); GitLab keeps the workspace's own URL and askpass."""
    root, _bares = upstream_repo

    async def github_only(host):
        if host != "github.com":
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return HEADER

    upstream = HttpBackendUpstream(root)
    process, serve, port_file, _link = await _run_channel(
        tmp_path, upstream, credential_source=github_only
    )
    try:
        base = {"GIT_ASKPASS": "/workspace/askpass"}
        proxy = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET, base)
        rewritten = {
            proxy[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(proxy["GIT_CONFIG_COUNT"]))
            if proxy[f"GIT_CONFIG_KEY_{i}"].endswith(".insteadOf")
        }
        assert rewritten == set(workspace_tools.GIT_PROXY_REWRITES["github.com"])
        assert {**base, **proxy}["GIT_ASKPASS"] == "/workspace/askpass"
        env = _client_env(tmp_path, port_file)
        clone = tmp_path / "clone"
        await _in_thread(
            _git, "clone", "https://github.com/acme/widgets.git", str(clone), cwd=tmp_path, env=env
        )
        (clone / "NEW").write_text("x\n")
        await _in_thread(_git, "add", "NEW", cwd=clone, env=env)
        await _in_thread(_git, "commit", "-m", "x", cwd=clone, env=env)
        pushed = await _in_thread(_git, "push", "origin", "main", cwd=clone, env=env, check=False)
        assert pushed.returncode != 0 and "is a protected branch" in pushed.stderr
        # GitLab is not routed: git tries it directly (offline here) and fails.
        direct = await _in_thread(
            _git,
            "ls-remote",
            "https://gitlab.com/group/sub/proj.git",
            cwd=tmp_path,
            env=env,
            check=False,
        )
        assert direct.returncode != 0
        assert not any(urlsplit(c["url"]).hostname == "gitlab.com" for c in upstream.calls)
    finally:
        await _stop(process, serve)


# ── Channel units ────────────────────────────────────────────────────────


class FrameLink:
    """A link whose guest side is driven directly by the test."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.frames: list[dict] = []
        #: ``credit`` frames, kept apart so answers read as before.
        self.credits: list[dict] = []

    async def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            frame = json.loads(line)
            (self.credits if frame.get("t") == "credit" else self.frames).append(frame)

    def guest(self, frame: dict) -> None:
        self.stdout.feed_data((json.dumps(frame) + "\n").encode())

    def get(self, rid: int, path: str) -> None:
        self.guest({"t": "req", "id": rid, "method": "GET", "path": path, "headers": {}})
        self.guest({"t": "end", "id": rid})


@pytest.mark.asyncio
async def test_an_overlong_frame_ends_the_channel_and_closes_the_guest_input():
    """Not a crash that leaves the guest routing lent hosts into a dead channel."""
    link = FrameLink()
    link.stdout = asyncio.StreamReader(limit=1024)
    closed: list[bool] = []
    link.close_input = lambda: closed.append(True)  # type: ignore[attr-defined]

    async def credential(host):
        return HEADER

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        http_session=HttpBackendUpstream(Path("/nonexistent")),
    )
    serve = asyncio.ensure_future(channel.serve())
    link.get(1, "/github.com/o/r.git/info/refs?x=" + "a" * 4096)
    await async_wait_until(serve.done, describe=lambda: "channel still serving")
    await serve
    assert closed == [True]


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
        assert link.frames[0]["message"] == (
            "Kiro Crew: governance does not allow dev.azure.com here"
        )
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


OLD, NEW = "a" * 40, "b" * 40
ZERO = "0" * 40


def _pkt(line: bytes) -> bytes:
    return b"%04x" % (len(line) + 4) + line


def _commands(*refs: str, new: str = NEW, caps: bytes = b"report-status side-band-64k") -> bytes:
    lines = [f"{OLD} {new} {ref}".encode() for ref in refs]
    if lines and caps:
        lines[0] += b"\0" + caps
    return b"".join(_pkt(line + b"\n") for line in lines) + b"0000"


def test_push_commands_are_read_up_to_the_flush_however_the_body_arrives():
    body = _pkt(b"shallow " + b"3" * 40) + _commands("refs/heads/a", "refs/tags/v1") + b"PACK.."
    whole = git_gateway.PushCommands()
    assert whole.feed(body) is True
    assert whole.refs == ["refs/heads/a", "refs/tags/v1"]
    assert bytes(whole.buffered) == body  # everything read goes upstream, pack included
    trickled = git_gateway.PushCommands()
    assert [trickled.feed(body[i : i + 1]) for i in range(len(body))][-1] is True
    assert trickled.refs == whole.refs


def test_a_probe_with_no_commands_is_admitted():
    """git's large-push auth probe POSTs a lone flush first."""
    probe = git_gateway.PushCommands()
    assert probe.feed(b"0000") is True and probe.refs == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"zzzz", id="non-hex-length"),
        pytest.param(b"0003", id="length-below-header"),
        pytest.param(b"0004", id="empty-pkt"),
        pytest.param(b"0001", id="v2-delimiter"),
        pytest.param(b"fff1" + b"x" * 65520, id="over-max-pkt"),
        pytest.param(_pkt(b"not a command\n") + b"0000", id="not-a-command"),
        pytest.param(_pkt(f"{OLD} {NEW} main\n".encode()) + b"0000", id="short-ref-name"),
        pytest.param(
            _pkt(f"{OLD.upper()} {NEW} refs/heads/a\n".encode()) + b"0000", id="upper-oid"
        ),
        pytest.param(_pkt(f"{OLD} {NEW} refs/heads/a b\n".encode()) + b"0000", id="space-in-ref"),
        pytest.param(
            _pkt(f"{OLD} {NEW} refs/heads/a\n".encode())
            + _pkt(f"{OLD} {NEW} refs/heads/b\0caps\n".encode())
            + b"0000",
            id="caps-on-second-command",
        ),
        pytest.param(
            _commands("refs/heads/a")[:-4] + _pkt(b"shallow " + b"3" * 40) + b"0000",
            id="shallow-after-command",
        ),
        pytest.param(
            _pkt(f"{OLD} {NEW} refs/heads/".encode() + b"\xff\n") + b"0000", id="non-utf8-ref"
        ),
        pytest.param(
            _pkt(b"push-cert\0caps\n") + _pkt(b"certificate version 0.1\n"), id="signed-push"
        ),
    ],
)
def test_a_push_whose_commands_cannot_be_read_is_refused(body):
    with pytest.raises(GitProxyRefusal) as refused:
        git_gateway.PushCommands().feed(body)
    assert refused.value.status == 403


def test_an_endless_command_list_is_refused_at_the_cap():
    reader = git_gateway.PushCommands()
    line = _pkt(f"{OLD} {NEW} refs/heads/{'x' * 200}\n".encode())
    with pytest.raises(GitProxyRefusal) as refused:
        for _ in range(git_gateway._MAX_PUSH_COMMANDS // len(line) + 2):
            reader.feed(line)
    assert refused.value.status == 413


@pytest.mark.parametrize(
    ("ref", "protected"),
    [
        ("refs/heads/main", True),
        ("refs/heads/MAIN", True),
        ("refs/heads/mainline", True),
        ("refs/remotes/origin/main", True),
        ("refs/heads/main-fix", False),
        ("refs/heads/feat/main", False),
        ("refs/tags/main", False),
        ("refs/heads/heads/main", False),
    ],
)
def test_the_proxy_protects_exactly_what_the_local_git_publish_check_does(ref, protected):
    from kiro_crew.security import _is_push_to_protected_branch

    assert git_gateway.is_protected_ref(ref) is protected
    local = _is_push_to_protected_branch(f"git push origin head:{ref}".lower())
    assert local is protected


def test_every_branch_the_local_check_protects_is_protected_in_the_proxy():
    for name in _protected_names():
        assert git_gateway.is_protected_ref(f"refs/heads/{name}")


def _demux(out: bytes, limit: int) -> dict[int, bytes]:
    """Split a side-band result into its bands, checking every packet's size."""
    bands: dict[int, bytes] = {1: b"", 2: b""}
    pos = 0
    while True:
        size = int(out[pos : pos + 4], 16)
        if size == 0:
            assert pos + 4 == len(out)
            return bands
        assert 5 <= size <= limit
        bands[out[pos + 4]] += out[pos + 5 : pos + size]
        pos += size


@pytest.mark.parametrize(("band", "limit"), [("side-band-64k", 65520), ("side-band", 1000)])
@pytest.mark.parametrize("length", [10, 990, 1000, 5000, 65300])
def test_a_rejection_stays_inside_the_negotiated_packet_size(band, limit, length):
    ref = f"refs/remotes/{'r' * length}/main"
    reader = git_gateway.PushCommands()
    assert reader.feed(_commands(ref, "refs/heads/feat/a", caps=f"report-status {band}".encode()))
    message = f"Kiro Crew: {ref} is a protected branch"
    bands = _demux(reader.rejection([ref], message), limit)
    assert bands[2] == message.encode() + b"\n"
    assert bands[1] == (
        _pkt(b"unpack ok\n")
        + _pkt(f"ng {ref} protected branch\n".encode())
        + _pkt(b"ng refs/heads/feat/a refused with the protected branch in the same push\n")
        + b"0000"
    )


def test_a_ref_too_long_to_report_on_gets_no_report():
    """Defensive: the caller then answers 403 rather than send a bad packet."""
    reader = git_gateway.PushCommands()
    reader.capabilities = frozenset({"report-status", "side-band-64k"})
    reader.refs = [f"refs/heads/{'x' * 65520}"]
    assert reader.rejection(reader.refs, "m") is None


class _RecordingUpstream(HttpBackendUpstream):
    def __init__(self) -> None:
        super().__init__(Path("/nonexistent"))
        self.bodies: list[bytes] = []

    async def answer(self, method, url, headers, data, allow_redirects):
        body = b""
        async for piece in data:
            body += piece
        self.bodies.append(body)
        return _Response(200, {}, b"")


def _channel(link, upstream):
    async def credential(host):
        return HEADER

    return GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=upstream,
    )


def _push(link, rid, body: bytes, headers=None, *, end=True) -> None:
    link.guest(
        {
            "t": "req",
            "id": rid,
            "method": "POST",
            "path": "/github.com/acme/w/git-receive-pack",
            "headers": headers or {"Content-Type": "application/x-git-receive-pack-request"},
        }
    )
    for start in range(0, len(body), 7):  # small frames: the reader must reassemble
        piece = body[start : start + 7]
        link.guest({"t": "body", "id": rid, "b": base64.b64encode(piece).decode()})
    if end:
        link.guest({"t": "end", "id": rid})


@pytest.mark.asyncio
async def test_the_channel_forwards_a_feature_push_byte_for_byte_and_refuses_the_rest():
    link, upstream = FrameLink(), _RecordingUpstream()
    serve = asyncio.ensure_future(_channel(link, upstream).serve())
    feature = _commands("refs/heads/feat/a", "refs/tags/v1") + b"PACK-bytes"
    try:
        _push(link, 1, feature)
        _push(link, 2, _commands("refs/heads/feat/b", "refs/heads/main") + b"PACK")
        _push(link, 3, _commands("refs/heads/main", new=ZERO))  # a delete
        _push(link, 4, _pkt(b"garbage\n") + b"0000")
        _push(link, 5, _commands("refs/heads/feat/c")[:-4])  # ends before its flush
        _push(link, 6, _commands("refs/heads/feat/d"), {"Content-Encoding": "gzip"})
        _push(link, 7, _commands("refs/heads/main", caps=b"report-status"))  # no side band
        _push(link, 8, _commands("refs/heads/main", caps=b""))  # no report asked for
        await async_wait_until(
            lambda: {f["id"] for f in link.frames if f["t"] in ("err", "end")} == set(range(1, 9)),
            describe=lambda: f"answers so far: {link.frames}",
        )
        errors = {f["id"]: f for f in link.frames if f["t"] == "err"}
        assert set(errors) == {4, 5, 6, 8}
        assert all(f["status"] == 403 for f in errors.values())
        for rid in (4, 5, 6):
            assert "could not be read" in errors[rid]["message"]
        assert "refs/heads/main is a protected branch" in errors[8]["message"]

        def answer(rid):
            frames = [f for f in link.frames if f["id"] == rid]
            assert frames[0] == {
                "t": "resp",
                "id": rid,
                "status": 200,
                "headers": {"Content-Type": "application/x-git-receive-pack-result"},
            }
            return b"".join(base64.b64decode(f["b"]) for f in frames if f["t"] == "body")

        # Refused as a server hook refuses: every command gets "ng", so git
        # names each ref and prints the reason on a "remote:" line.
        message = b"refs/heads/main is a protected branch"
        two = answer(2)
        assert message in two and two.endswith(b"0000")
        assert b"ng refs/heads/main protected branch\n" in two
        assert b"ng refs/heads/feat/b refused with the protected branch" in two
        assert b"ng refs/heads/main protected branch\n" in answer(3)
        assert answer(7) == (
            _pkt(b"unpack ok\n") + _pkt(b"ng refs/heads/main protected branch\n") + b"0000"
        )
        assert upstream.bodies == [feature]  # nothing of a refused push went upstream
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_every_push_decision_writes_one_sel_row(monkeypatch):
    import kiro_crew.sel as sel_module

    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kwargs):
            rows.append(kwargs)

    monkeypatch.setattr(sel_module, "sel", lambda: _Sel())
    link, upstream = FrameLink(), _RecordingUpstream()
    serve = asyncio.ensure_future(_channel(link, upstream).serve())
    try:
        _push(link, 1, _commands("refs/heads/feat/a") + b"PACK")
        _push(link, 2, _commands("refs/heads/feat/b", "refs/heads/main") + b"PACK")
        _push(link, 3, _pkt(b"garbage\n") + b"0000")
        await async_wait_until(
            lambda: {f["id"] for f in link.frames if f["t"] in ("err", "end")} == {1, 2, 3},
            describe=lambda: f"answers so far: {link.frames}",
        )
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")
    decided = sorted((r["outcome"], r["rule"], r["item"]) for r in rows)
    assert decided == [
        ("allowed", "no-protected-branch", "github.com/acme/w refs/heads/feat/a"),
        ("denied", "protected-branch", "github.com/acme/w refs/heads/feat/b refs/heads/main"),
        ("denied", "unreadable-push", "github.com/acme/w"),
    ]
    assert {(r["session_key"], r["agent"], r["scope"], r["tool_name"]) for r in rows} == {
        ("dashboard:x", "researcher", "git.push", "git_proxy")
    }


def test_a_huge_rejection_in_the_small_side_band_is_well_formed():
    """About 90k refs fit under the command cap: the report for that many,
    split into 1000-byte side-band packets, still names every ref."""
    reader = git_gateway.PushCommands()
    refs = [f"refs/remotes/r{i}/main" for i in range(60000)]
    reader.refs = refs
    reader.capabilities = frozenset({"report-status", "side-band"})
    report = reader.rejection(refs, "m")
    assert report is not None
    bands = _demux(report, 1000)
    assert bands[1].count(b"ng refs/remotes/") == len(refs)


def test_the_rejection_report_looks_refs_up_in_a_set_not_the_callers_list():
    """Tens of thousands of refs fit under the command cap; a list lookup per
    ref made the report quadratic and stalled the event loop for seconds."""

    class _Counting(list):
        lookups = 0

        def __contains__(self, item):
            type(self).lookups += 1
            return super().__contains__(item)

    reader = git_gateway.PushCommands()
    refs = [f"refs/remotes/r{i}/main" for i in range(2000)]
    assert reader.feed(_commands(*refs, caps=b"report-status side-band-64k"))
    refused = _Counting(refs)
    report = reader.rejection(refused, "m")
    assert report is not None and _Counting.lookups == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "agent", "rule"),
    [
        pytest.param(
            "/other.example/owner/repo/info/refs?service=git-upload-pack",
            "researcher",
            "not-proxied",
            id="host-not-proxied",
        ),
        pytest.param(
            "/github.com/acme/w/info/refs?service=git-upload-pack", "  ", "no-agent", id="no-agent"
        ),
    ],
)
async def test_a_request_refused_before_the_egress_check_writes_a_sel_row(
    monkeypatch, path, agent, rule
):
    import kiro_crew.sel as sel_module

    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kwargs):
            rows.append(kwargs)

    monkeypatch.setattr(sel_module, "sel", lambda: _Sel())
    link = FrameLink()

    async def credential(host):
        raise AssertionError("no credential may be read for a refused request")

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent=agent,
        egress_permitted=lambda key, host, a: True,
        logins_lent=lambda: True,
        http_session=_RecordingUpstream(),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.get(1, path)
        await async_wait_until(lambda: link.frames, describe=lambda: "refusal")
        assert link.frames[0]["t"] == "err" and link.frames[0]["status"] == 403
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")
    assert [(r["outcome"], r["rule"], r["scope"]) for r in rows] == [
        ("denied", rule, "git.request")
    ]


@pytest.mark.asyncio
async def test_a_strict_push_refusal_writes_a_sel_row_and_a_failed_write_changes_nothing(
    monkeypatch,
):
    import kiro_crew.sel as sel_module

    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kwargs):
            rows.append(kwargs)
            raise OSError("SEL unwritable")  # the refusal must still be answered

    monkeypatch.setattr(sel_module, "sel", lambda: _Sel())
    link = FrameLink()
    channel = _channel(link, _RecordingUpstream())
    channel._logins_lent = lambda: False
    serve = asyncio.ensure_future(channel.serve())
    try:
        _push(link, 1, _commands("refs/heads/feat/a") + b"PACK")
        await async_wait_until(lambda: link.frames, describe=lambda: "strict refusal")
        assert link.frames[0]["t"] == "err" and link.frames[0]["status"] == 403
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")
    assert [(r["outcome"], r["rule"]) for r in rows] == [("denied", "strict-sandbox")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lent", "logins", "published"),
    [
        pytest.param(False, set(HOSTS), [], id="strict-lends-nothing"),
        pytest.param(True, {"github.com"}, ["github.com"], id="only-signed-in-hosts"),
        pytest.param(True, set(), [], id="no-login-anywhere"),
    ],
)
async def test_the_gateway_publishes_only_the_hosts_it_can_lend_for(lent, logins, published):
    link, reads = FrameLink(), []

    async def credential(host):
        reads.append(host)
        if host not in logins:
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return HEADER

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: lent,
        http_session=_RecordingUpstream(),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        assert link.frames == []  # nothing is published until the guest asks
        link.guest({"t": "lend", "id": 0})
        # A host found on the first round is published at once, marked partial,
        # then the full list.
        full = {"t": "lend", "id": 0, "hosts": published}
        expected = ([{**full, "partial": True}] if published else []) + [full]
        await async_wait_until(
            lambda: len(link.frames) >= len(expected), describe=lambda: "lendable hosts"
        )
        assert link.frames == expected
        # Under strict no credential source is even consulted.
        assert reads == ([] if not lent else list(HOSTS))
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_a_slow_lookup_does_not_withhold_another_hosts_login(monkeypatch):
    """A credential CLI running to its time limit for one host must not keep an
    available login for another host out of the first list the guest waits for."""
    monkeypatch.setattr(git_gateway, "LEND_REFRESH_SECS", 3600.0)
    link, release = FrameLink(), asyncio.Event()

    async def credential(host):
        if host == "github.com":
            return HEADER
        await release.wait()  # every other host's lookup hangs until released
        raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=_RecordingUpstream(),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: link.frames, describe=lambda: "early lend")
        # GitHub is out while the other lookups are still running, marked
        # partial so a waiting command keeps waiting for the rest.
        assert link.frames == [{"t": "lend", "id": 0, "hosts": ["github.com"], "partial": True}]
        release.set()
        await async_wait_until(lambda: len(link.frames) == 2, describe=lambda: "full lend")
        assert link.frames[-1] == {"t": "lend", "id": 0, "hosts": ["github.com"]}
        # A refresh publishes only its full list, never a partial one.
        channel._lend_stale.set()
        await async_wait_until(lambda: len(link.frames) == 3, describe=lambda: "refresh")
        assert link.frames[-1] == {"t": "lend", "id": 0, "hosts": ["github.com"]}
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_a_rejected_gateway_login_is_republished_at_once(monkeypatch):
    """After an upstream 401 the guest hears the new list without waiting out
    the refresh, so the next command goes direct."""
    monkeypatch.setattr(git_gateway, "LEND_REFRESH_SECS", 3600.0)
    link, valid = FrameLink(), {"github.com"}

    async def credential(host):
        if host not in valid:
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return HEADER

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=HttpBackendUpstream(Path("/nonexistent"), status=401),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: link.frames, describe=lambda: "first list")
        valid.clear()  # the stored login stops working
        link.get(1, "/github.com/acme/w/info/refs?service=git-receive-pack")
        await async_wait_until(
            lambda: [f for f in link.frames if f["t"] == "lend"][-1]["hosts"] == [],
            describe=lambda: f"frames: {link.frames}",
        )
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_a_revoked_login_its_source_still_returns_stops_being_lent(monkeypatch):
    """`gh auth token` can keep printing a token the host has revoked. The same
    refused value must not keep the host routed here; a fresh sign-in (a new
    value) lends again."""
    monkeypatch.setattr(git_gateway, "LEND_REFRESH_SECS", 3600.0)
    link, token = FrameLink(), {"value": HEADER}

    async def credential(host):
        if host != "github.com":
            raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found")
        return token["value"]

    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        logins_lent=lambda: True,
        http_session=HttpBackendUpstream(Path("/nonexistent"), status=401),
    )
    serve = asyncio.ensure_future(channel.serve())

    def last_lend():
        lends = [f for f in link.frames if f["t"] == "lend"]
        return lends[-1]["hosts"] if lends else None

    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: last_lend() == ["github.com"], describe=lambda: "lent")
        link.get(1, "/github.com/acme/w/info/refs?service=git-receive-pack")
        await async_wait_until(lambda: last_lend() == [], describe=lambda: f"{link.frames}")
        token["value"] = "Basic " + base64.b64encode(b"x-access-token:fresh").decode()
        channel._lend_stale.set()
        await async_wait_until(lambda: last_lend() == ["github.com"], describe=lambda: "re-lent")
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_a_refusal_stops_reading_its_body_before_it_answers():
    """Answering while the body queue is still registered can deadlock the link
    (serve blocks in put, the reader pauses, the answer's acks go unread)."""
    channel_ref: list = []
    registered_at_answer: dict[int, bool] = {}

    class _Link(FrameLink):
        async def write(self, data: bytes) -> None:
            for line in data.decode().splitlines():
                frame = json.loads(line)
                if frame["t"] in ("err", "resp"):
                    registered_at_answer[frame["id"]] = frame["id"] in channel_ref[0]._requests
            await super().write(data)

    link = _Link()
    channel = _channel(link, _RecordingUpstream())
    channel_ref.append(channel)
    serve = asyncio.ensure_future(channel.serve())
    try:
        # A protected push (report answer), an unreadable one (403), each with
        # body still arriving after the refusal decision.
        _push(link, 1, _commands("refs/heads/main") + b"P" * 4000, end=False)
        _push(link, 2, _pkt(b"garbage\n") + b"0000" + b"P" * 4000, end=False)
        await async_wait_until(
            lambda: set(registered_at_answer) == {1, 2},
            describe=lambda: f"answers: {link.frames}",
        )
        assert registered_at_answer == {1: False, 2: False}
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


class _FailingUpstream(HttpBackendUpstream):
    async def answer(self, method, url, headers, data, allow_redirects):
        raise RuntimeError("upstream broke mid-upload")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upstream",
    [
        pytest.param(lambda: _FailingUpstream(Path("/nonexistent")), id="upstream-error"),
        pytest.param(lambda: HttpBackendUpstream(Path("/nonexistent"), status=401), id="push-401"),
        pytest.param(lambda: HttpBackendUpstream(Path("/nonexistent"), status=302), id="redirect"),
    ],
)
async def test_an_upstream_failure_stops_reading_the_push_body_before_it_answers(upstream):
    channel_ref: list = []
    registered_at_answer: dict[int, bool] = {}

    class _Link(FrameLink):
        async def write(self, data: bytes) -> None:
            for line in data.decode().splitlines():
                frame = json.loads(line)
                if frame["t"] == "err":
                    registered_at_answer[frame["id"]] = frame["id"] in channel_ref[0]._requests
            await super().write(data)

    link = _Link()
    channel = _channel(link, upstream())
    channel_ref.append(channel)
    serve = asyncio.ensure_future(channel.serve())
    try:
        _push(link, 1, _commands("refs/heads/feat/a") + b"P" * 4000, end=False)
        await async_wait_until(
            lambda: 1 in registered_at_answer, describe=lambda: f"answers: {link.frames}"
        )
        assert registered_at_answer == {1: False}
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


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


# ── The strict sandbox tier lends no gateway login ───────────────────────


@pytest.mark.parametrize(
    ("tier", "floor", "lent"),
    [
        ("strict", None, False),
        ("standard", None, True),
        ("auto", None, True),
        ("cc", None, True),
        ("off", None, True),
        # A governance floor of strict wins over a looser configured tier.
        ("standard", "strict", False),
    ],
)
def test_gateway_logins_follow_the_sandbox_tier(monkeypatch, tier, floor, lent):
    from kiro_crew import sandbox

    _set_tier(tier)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: floor)
    assert git_gateway.gateway_logins_lent() is lent


def _set_tier(tier: str) -> None:
    from kiro_crew.config.loader import config_dir

    (config_dir() / "config.json").write_text(
        json.dumps({"agent": {"sandbox": tier}}), encoding="utf-8"
    )


def test_the_tier_and_its_readability_come_from_one_config_load(monkeypatch):
    """Two loads could pair a readable first snapshot with a defaulted second
    one (a save landing between them); one load answers both."""
    from kiro_crew import sandbox
    from kiro_crew.config.loader import KiroCrewConfig

    _set_tier("strict")
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    loads = []
    real = KiroCrewConfig.load.__func__

    def counted(cls, *args, **kwargs):
        loads.append(1)
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(counted))
    assert git_gateway.gateway_logins_lent() is False
    assert len(loads) == 1


def test_an_unreadable_sandbox_tier_lends_nothing(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    def broken(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", broken)
    assert git_gateway.gateway_logins_lent() is False


@pytest.mark.parametrize(
    ("document", "lent"),
    [
        pytest.param('{"agent": {"sandbox": "standard"}}', True, id="readable"),
        pytest.param("{not json", False, id="unparseable-config"),
        pytest.param("[]", False, id="top-level-not-an-object"),
    ],
)
def test_a_config_that_cannot_be_read_lends_nothing(monkeypatch, document, lent):
    """The loader stands defaults in for an unreadable config instead of raising,
    and the default tier is not strict: the proxy must not read that as a tier."""
    from kiro_crew import sandbox
    from kiro_crew.config.loader import config_dir

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    (config_dir() / "config.json").write_text(document, encoding="utf-8")
    assert git_gateway.gateway_logins_lent() is lent


async def _tiered_channel(monkeypatch, tier, upstream):
    """A channel on the default tier reader, the configured tier set to ``tier``."""
    from kiro_crew import sandbox

    _set_tier(tier)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
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
        http_session=upstream,
    )
    return link, reads, asyncio.ensure_future(channel.serve())


@pytest.mark.asyncio
@pytest.mark.parametrize(("tier", "lent"), [("strict", False), ("standard", True)])
async def test_strict_tier_reads_anonymously_and_refuses_pushes(monkeypatch, tier, lent):
    upstream = HttpBackendUpstream(Path("/nonexistent"), status=200)
    link, reads, serve = await _tiered_channel(monkeypatch, tier, upstream)
    try:
        link.get(1, "/github.com/acme/w/info/refs?service=git-upload-pack")
        await async_wait_until(
            lambda: any(f["t"] == "end" for f in link.frames), describe=lambda: "fetch answered"
        )
        assert ("Authorization" in upstream.calls[0]["headers"]) is lent
        assert reads == (["github.com"] if lent else [])
        link.get(2, "/github.com/acme/w/info/refs?service=git-receive-pack")
        # Even a feature branch: strict lends no login for any push.
        _push(link, 3, _commands("refs/heads/feat/a") + b"PACK")
        await async_wait_until(
            lambda: {f["id"] for f in link.frames if f["t"] in ("err", "end")} >= {2, 3},
            describe=lambda: "push answered",
        )
        push = [f for f in link.frames if f["id"] in (2, 3)]
        if lent:
            assert [f["t"] for f in push if f["t"] in ("err", "end")] == ["end", "end"]
        else:
            # Refused before any credential read and before anything goes upstream.
            assert reads == []
            assert sorted(push, key=lambda f: f["id"]) == [
                {
                    "t": "err",
                    "id": rid,
                    "status": 403,
                    "message": git_gateway._strict_refusal(
                        "a push to github.com/acme/w", unverifiable=False
                    ),
                }
                for rid in (2, 3)
            ]
            assert len(upstream.calls) == 1
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
async def test_strict_tier_names_itself_when_a_private_read_needs_a_login(monkeypatch):
    upstream = HttpBackendUpstream(Path("/nonexistent"), status=401)
    link, reads, serve = await _tiered_channel(monkeypatch, "strict", upstream)
    try:
        link.get(1, "/gitlab.com/group/proj/info/refs?service=git-upload-pack")
        await async_wait_until(lambda: link.frames, describe=lambda: "read answered")
        assert link.frames == [
            {
                "t": "err",
                "id": 1,
                "status": 403,
                "message": git_gateway._strict_refusal(
                    "reading gitlab.com/group/proj", unverifiable=False
                ),
            }
        ]
        # Under strict the proxy drops workspace credentials, so no other login helps.
        assert "not available under the strict sandbox" in link.frames[0]["message"]
        assert reads == [] and "Authorization" not in upstream.calls[0]["headers"]
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")


@pytest.mark.asyncio
@pytest.mark.parametrize("lent", [False, True])
async def test_readiness_reports_withheld_logins_under_strict(monkeypatch, lent):
    async def status(host):
        return "signed_in"

    monkeypatch.setattr(git_gateway, "gateway_logins_lent", lambda: lent)
    monkeypatch.setattr(git_gateway, "login_status", status)
    expected = "signed_in" if lent else "withheld"
    assert await git_gateway.git_readiness() == {host: expected for host in HOSTS}


@pytest.mark.asyncio
async def test_an_unverifiable_windows_gateway_lends_no_login_and_runs_no_source(monkeypatch):
    """A Windows gateway that cannot read its own SID or file ACLs lends nothing:
    every lend path refuses before any credential source runs, with one actionable
    message, and a push is refused whole. Fails if the refusal is undone."""
    import kiro_crew.sel as sel_module

    monkeypatch.setattr(git_gateway, "lending_unverifiable", lambda: True)
    _set_tier("standard")  # a tier that would lend everywhere else
    ran: list[str] = []

    def source(name):
        async def run(host):
            ran.append(name)
            return HEADER

        return run

    for name in list(git_gateway._SOURCES):
        monkeypatch.setitem(git_gateway._SOURCES, name, source(name))

    async def status(host):
        ran.append(f"status:{host}")
        return "signed_in"

    monkeypatch.setattr(git_gateway, "login_status", status)

    def no_git():
        ran.append("git")
        return None

    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", no_git)

    assert git_gateway.gateway_logins_lent() is False
    assert await git_gateway.git_readiness() == {host: "unsupported" for host in HOSTS}
    with pytest.raises(GitProxyRefusal) as refused:
        await host_credential("github.com")
    assert refused.value.status == 502
    assert refused.value.message == git_gateway.WINDOWS_LENDING_UNVERIFIED
    with pytest.raises(git_gateway._NoCredential) as no_credential:
        await git_gateway._from_git_credential("bitbucket.org")
    assert no_credential.value.hint == git_gateway.WINDOWS_LENDING_UNVERIFIED
    assert ran == []

    rows: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kwargs):
            rows.append(kwargs)

    monkeypatch.setattr(sel_module, "sel", lambda: _Sel())
    link = FrameLink()
    # The channel's default login check: the one a real session uses.
    channel = GitProxyChannel(
        link,
        "dashboard:x",
        credential_source=host_credential,
        agent="researcher",
        egress_permitted=lambda key, host, agent: True,
        http_session=_RecordingUpstream(),
    )
    serve = asyncio.ensure_future(channel.serve())
    try:
        link.guest({"t": "lend", "id": 0})
        await async_wait_until(lambda: link.frames, describe=lambda: "lend list")
        assert link.frames[0] == {"t": "lend", "id": 0, "hosts": []}
        _push(link, 1, _commands("refs/heads/feat/a") + b"PACK")
        await async_wait_until(lambda: len(link.frames) > 1, describe=lambda: "push refusal")
        refusal = link.frames[1]
        assert refusal["t"] == "err" and refusal["status"] == 403
        assert refusal["message"].startswith(git_gateway.WINDOWS_LENDING_UNVERIFIED)
    finally:
        link.stdout.feed_eof()
        await await_until(serve, what="gateway channel closed")
    assert [(r["outcome"], r["rule"]) for r in rows] == [("denied", "windows-unverified")]
    assert ran == []
